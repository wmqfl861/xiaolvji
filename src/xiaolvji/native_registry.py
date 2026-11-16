"""Controller-owned native acceptance records with fresh identity checks.

A record cannot supply a caller-selected success flag. The registration path
runs the fixed local native fixture itself, under the resource-domain lock.
Records certify protocol compatibility, never real-provider or final quality
acceptance. Workers cannot read or write this controller directory.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re

from .authority import atomic_json
from .contracts import Fault, canonical_json, digest, read_json, validate_identifier
from .native_acceptance import ASSERTIONS, verify_native_host
from .native_policy import (capture_native_identity, capture_project_policy,
                            normalize_native_deployment)


class NativeRegistry:
    def __init__(self, state_dir):
        self.state = Path(state_dir).absolute()
        if self.state.resolve() != self.state:
            raise Fault("UNSAFE_STATE", "Native registry state cannot contain path aliases.")
        self.root = self.state / "native-hosts"

    def _path(self, project_id, engine):
        validate_identifier(project_id, "native deployment project")
        if engine not in ("codex", "zcode"):
            raise Fault("CAPABILITY_UNSUPPORTED", "Unknown native deployment engine.")
        key = hashlib.sha256((project_id + ":" + engine).encode()).hexdigest()
        return self.root / (key + ".json")

    @staticmethod
    def _report(report, snapshot, deployment):
        fields = {"schema_version", "status", "kind", "engine", "protocol", "assertions",
                  "host_snapshot_digest", "deployment_digest", "native_host_invocations",
                  "synthetic_provider_requests", "real_provider_requests", "authentication_attempted",
                  "native_result", "semantic_error", "executor_identity", "sandbox_policy_digest",
                  "elapsed_seconds"}
        def is_digest(value):
            return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
        if (not isinstance(report, dict) or set(report) != fields or
                type(report.get("schema_version")) is not int or report["schema_version"] != 1 or
                report.get("status") != "passed" or
                report.get("kind") != "native_protocol_fixture" or
                report.get("engine") != deployment.value["engine"] or
                report.get("protocol") != deployment.value["protocol"] or
                report.get("host_snapshot_digest") != digest(snapshot) or
                report.get("deployment_digest") != deployment.digest or
                type(report.get("native_host_invocations")) is not int or report["native_host_invocations"] != 1 or
                type(report.get("synthetic_provider_requests")) is not int or report["synthetic_provider_requests"] != 2 or
                type(report.get("real_provider_requests")) is not int or report["real_provider_requests"] != 0 or
                report.get("authentication_attempted") is not False or
                report.get("semantic_error") is not None or
                type(report.get("elapsed_seconds")) not in (int, float) or
                not 0 <= report["elapsed_seconds"] <= 86400 or not math.isfinite(report["elapsed_seconds"]) or
                not is_digest(report.get("sandbox_policy_digest")) or
                not isinstance(report.get("assertions"), dict) or
                set(report["assertions"]) != ASSERTIONS or
                any(value is not True for value in report["assertions"].values())):
            raise Fault("NATIVE_ACCEPTANCE_FAILED", "The complete native protocol and boundary acceptance did not pass.")
        semantic = report["native_result"]
        semantic_fields = {"schema_version", "engine", "status", "session_id", "turn_id", "event_count",
                           "terminal_type", "stdout_digest", "stdout_bytes", "stdout_truncated",
                           "process_tree_terminated"}
        if (not isinstance(semantic, dict) or set(semantic) != semantic_fields or
                type(semantic.get("schema_version")) is not int or semantic["schema_version"] != 1 or
                semantic.get("engine") != report["engine"] or semantic.get("status") != "completed" or
                not isinstance(semantic.get("session_id"), str) or not 1 <= len(semantic["session_id"]) <= 256 or
                semantic.get("turn_id") is not None or semantic.get("terminal_type") != "turn.completed" or
                type(semantic.get("event_count")) is not int or not 3 <= semantic["event_count"] <= 65536 or
                type(semantic.get("stdout_bytes")) is not int or not 1 <= semantic["stdout_bytes"] <= 4 * 1024 * 1024 or
                not is_digest(semantic.get("stdout_digest")) or semantic.get("stdout_truncated") is not False or
                semantic.get("process_tree_terminated") is not False):
            raise Fault("NATIVE_ACCEPTANCE_FAILED", "Native acceptance lacks complete semantic evidence.")
        identity = report["executor_identity"]
        identity_fields = {"attempt_id", "boot_id", "generation", "nonce", "pid", "pid_namespace", "proc_pid",
                           "profile", "sandbox_policy_digest", "session_id", "start_time", "tracking"}
        if (not isinstance(identity, dict) or set(identity) != identity_fields or
                identity.get("attempt_id") != "native-probe" or
                type(identity.get("generation")) is not int or identity["generation"] != 1 or
                identity.get("profile") != "controlled_executor" or
                identity.get("tracking") != "linux_child_subreaper_v1" or
                identity.get("sandbox_policy_digest") != report["sandbox_policy_digest"] or
                any(type(identity.get(key)) is not int or identity[key] < 1 for key in ("pid", "proc_pid", "session_id")) or
                any(not isinstance(identity.get(key), str) or not 1 <= len(identity[key]) <= 256
                    for key in ("boot_id", "nonce", "pid_namespace", "start_time"))):
            raise Fault("NATIVE_ACCEPTANCE_FAILED", "Native acceptance lacks a matching strict executor identity.")

    async def verify(self, project_id, project_root, deployment, configuration, output):
        """Verify an explicitly configured standalone host; never read a key."""
        engine = deployment.value["engine"]
        target = self._path(project_id, engine)
        snapshot = configuration.host(project_id, engine)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state / "dispatcher.lock"
        if lock_path.is_symlink():
            raise Fault("UNSAFE_STATE", "Native acceptance lock cannot be a symlink.")
        try:
            import fcntl
        except ImportError:
            raise Fault("CAPABILITY_UNSUPPORTED", "Native acceptance requires POSIX controller locking.") from None
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Fault("CONTROLLER_RUNNING", "Stop the dispatcher before running native acceptance.") from None
            try:
                database = self.state / "ledger.sqlite3"
                if database.is_file():
                    from .store import Ledger
                    ledger = Ledger(database)
                    if ledger.active_attempts():
                        raise Fault("INSTANCE_UNCERTAIN", "Native acceptance requires all existing managed attempts to be reconciled first.")
                before = await asyncio.to_thread(capture_native_identity, deployment)
                project_policy = await asyncio.to_thread(capture_project_policy, project_root)
                report = await verify_native_host(deployment, snapshot, output)
                self._report(report, snapshot, deployment)
                after = await asyncio.to_thread(capture_native_identity, deployment)
                latest_project = await asyncio.to_thread(capture_project_policy, project_root)
                if before != after or latest_project["policy_digest"] != project_policy["policy_digest"]:
                    raise Fault("NATIVE_IDENTITY_CHANGED", "Native code or policy changed during acceptance.")
                record = {"schema_version": 1, "project_id": project_id, "engine": engine,
                          "configuration_digest": configuration.digest, "host_snapshot_digest": digest(snapshot),
                          "deployment": deployment.value, "identity": after,
                          "project_policy_digest": latest_project["policy_digest"],
                          "report": report, "report_digest": digest(report),
                          "verified_at": datetime.now(timezone.utc).isoformat()}
                atomic_json(target, record)
                return {"registered": True, "project_id": project_id, "engine": engine,
                        "native_protocol_verified": True, "real_provider_verified": False,
                        "record_digest": digest(record), "report": report}
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def check(self, project_id, engine, project_root, configuration, *, workspace=None):
        """Check the exact record and current code/policy before every launch."""
        path = self._path(project_id, engine)
        if path.is_symlink() or self.root.is_symlink() or not path.is_file():
            raise Fault("NATIVE_NOT_VERIFIED", "No controller-owned native acceptance matches this project and engine.")
        record = read_json(path)
        fields = {"schema_version", "project_id", "engine", "configuration_digest", "host_snapshot_digest",
                  "deployment", "identity", "project_policy_digest", "report", "report_digest", "verified_at"}
        if (not isinstance(record, dict) or set(record) != fields or type(record.get("schema_version")) is not int or
                record["schema_version"] != 1 or record["project_id"] != project_id or record["engine"] != engine):
            raise Fault("NATIVE_NOT_VERIFIED", "Native acceptance record has an unsupported binding.")
        deployment = normalize_native_deployment(record["deployment"])
        snapshot = configuration.host(project_id, engine)
        if record["configuration_digest"] != configuration.digest or record["host_snapshot_digest"] != digest(snapshot):
            raise Fault("NATIVE_IDENTITY_CHANGED", "Provider or host configuration changed after native acceptance.")
        if record["report_digest"] != digest(record["report"]):
            raise Fault("NATIVE_NOT_VERIFIED", "Native acceptance report digest is inconsistent.")
        self._report(record["report"], snapshot, deployment)
        current = capture_native_identity(deployment)
        if canonical_json(current) != canonical_json(record["identity"]):
            raise Fault("NATIVE_IDENTITY_CHANGED", "Native binary, runtime, platform, or policy changed after acceptance.")
        policy = capture_project_policy(project_root, workspace=workspace)
        if policy["policy_digest"] != record["project_policy_digest"]:
            raise Fault("NATIVE_IDENTITY_CHANGED", "Project policy changed after native acceptance.")
        return {"deployment": deployment, "identity": current, "record_digest": digest(record),
                "project_policy_digest": policy["policy_digest"], "host_snapshot": snapshot,
                "instructions": policy["instructions"],
                "native_protocol_verified": True, "real_provider_verified": False}
