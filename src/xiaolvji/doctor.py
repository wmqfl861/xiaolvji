"""Local, bounded capability inspection without authentication or trust changes."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from .adapters import ZCODE_SOURCE_COMMIT
from .contracts import Fault


def _inspect_cli(engine: str, executable: str | None = None) -> dict:
    executable = executable or shutil.which(engine)
    result = {"engine": engine, "executable": executable, "version": None,
              "status": "unknown", "source_commit": ZCODE_SOURCE_COMMIT if engine == "zcode" else None,
              "integration_tested": False, "required_flags_detected": False}
    if executable is None:
        result.update(status="unsupported", reason="CLI executable was not found on PATH.")
        return result
    try:
        # Temporary files keep unexpected help output out of unbounded buffers.
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, timeout=4, check=False)
            output.seek(0)
            version = output.read(1024).decode("utf-8", "replace").strip()
        if completed.returncode != 0 or not version:
            result.update(status="unknown", reason="The CLI version probe failed.")
            return result
        result["version"] = version[:240]
        help_argv = [executable, "exec", "--help"] if engine == "codex" else [executable, "--help"]
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(help_argv, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, timeout=4, check=False)
            output.seek(0)
            help_text = output.read(131_072).decode("utf-8", "replace")
        required = ("--json", "--sandbox", "--cd", "--config") if engine == "codex" else (
            "--cwd", "--prompt", "--output-format", "--workflow-mode", "--mode")
        missing = [flag for flag in required if flag not in help_text]
        result.update(status="detected", required_flags_detected=completed.returncode == 0 and not missing,
                      missing_help_flags=missing)
        result["reason"] = "Version/help detected; native hooks, configuration enforcement, authentication, and worker execution still require host integration testing."
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update(status="unknown", reason="The bounded local CLI probe failed: " + type(exc).__name__)
    return result


def doctor(engine: str | None = None, profile: str = "controlled_executor", *, provider_configuration=None,
           native_registry=None, project_roots=None) -> dict:
    if engine not in (None, "codex", "zcode"):
        raise Fault("CAPABILITY_UNSUPPORTED", "Doctor supports only local Codex and ZCode.")
    if profile not in ("controlled_executor", "cooperative_native"):
        raise Fault("INVALID_INPUT", "Unknown execution profile.")
    readiness = []
    selected = ((engine,) if engine else ("codex", "zcode"))
    if profile == "controlled_executor" and provider_configuration is not None:
        for binding in provider_configuration.value["bindings"]:
            if binding["host"]["engine"] not in selected:
                continue
            project_id, name = binding["project_id"], binding["host"]["engine"]
            item = {"project_id": project_id, "engine": name, "native_protocol_verified": False,
                    "real_provider_verified": False}
            try:
                if native_registry is None or project_id not in (project_roots or {}):
                    raise Fault("NATIVE_NOT_VERIFIED", "Enroll the project and run host verify for its exact standalone deployment.")
                ready = native_registry.check(project_id, name, project_roots[project_id], provider_configuration)
                item.update(native_protocol_verified=True, executable=ready["deployment"].value["executable"],
                            record_digest=ready["record_digest"])
            except Fault as exc:
                item["error"] = exc.as_dict()
            readiness.append(item)
    hosts = [_inspect_cli(name, next((item["executable"] for item in readiness
                if item["engine"] == name and item["native_protocol_verified"]), None)) for name in selected]
    reasons = []
    for host in hosts:
        if host["executable"] is None:
            reasons.append(host["engine"] + ": executable unavailable")
        elif not host["required_flags_detected"]:
            reasons.append(host["engine"] + ": required CLI flags were not detected")
    if sys.version_info < (3, 11):
        reasons.append("Python 3.11 or newer is required")
    if shutil.which("git") is None:
        reasons.append("Git executable unavailable")
    try:
        from .executor import ProcessExecutor
        with tempfile.TemporaryDirectory(prefix="xiaolvji-doctor-") as temporary:
            execution = ProcessExecutor(Path(temporary), profile=profile).probe()
    except (ImportError, OSError, Fault) as exc:
        execution = {"supported": False, "strict_supported": False,
                     "effective_mode": "blocked", "blocking_reasons": [type(exc).__name__ + ": executor probe unavailable"]}
    if execution.get("supported") is not True:
        reasons.extend(execution.get("blocking_reasons") or ["Executor capability is not established"])
    if profile == "controlled_executor" and execution.get("strict_supported") is not True:
        reasons.append("The required offline OS boundary is unavailable on this machine; strict execution is blocked")
    if profile == "controlled_executor":
        if provider_configuration is None:
            reasons.append("Strict model jobs require explicit provider profiles and isolated host snapshots; no ambient credentials or model settings are imported")
        for name in selected:
            matching = [item for item in readiness if item["engine"] == name]
            if not matching:
                reasons.append(name + ": no verified native deployment matches an explicit provider binding")
            for item in matching:
                if not item["native_protocol_verified"]:
                    reasons.append(name + "/" + item["project_id"] + ": " + item["error"]["message"])
    native_supported = (profile == "controlled_executor" and bool(readiness) and
                        all(item["native_protocol_verified"] for item in readiness) and
                        all(any(item["engine"] == name for item in readiness) for name in selected))
    return {
        "schema_version": 1,
        "execution_location": "local",
        "requested_mode": profile,
        "effective_mode": "blocked" if reasons else profile,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "host_capabilities": hosts,
        "native_deployments": readiness,
        "executor": execution,
        "offline_executor_supported": execution.get("strict_supported") is True,
        "model_transport": {"strict_supported": native_supported and execution.get("strict_supported") is True,
                            "status": "native_protocol_verified" if native_supported else "native_acceptance_required",
                            "kind": "inherited_socketpair_v1", "private_loopback": True,
                            "network_policy": ("brokered-provider-v1" if provider_configuration else "none") if profile == "controlled_executor" else "host_environment",
                            "configuration_supplied": provider_configuration is not None,
                            "configuration": provider_configuration.summary() if provider_configuration is not None else None,
                            "host_credentials_provisioned": False,
                            "remote_processing_after_disconnect": "unknown"},
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "checks": [
            {"name": "codex_agents_disabled", "status": "documented",
             "detail": "A registered Codex native fixture checks the actual tool manifest; unverified deployments remain blocked.",
             "source": "https://learn.chatgpt.com/docs/config-file/config-reference"},
            {"name": "zcode_agent_disallowlist", "status": "source_verified",
             "detail": "Pinned CLI supports the explicit disallowlist and guarded mode; arbitrary shell launching remains outside native-tool denial.",
             "source_commit": ZCODE_SOURCE_COMMIT},
            {"name": "native_hook_identity", "status": "unsupported",
             "detail": "Codex parent session IDs do not prove a child's identity; ordinary ZCode Agent children do not inherit parent hooks."},
            {"name": "native_hook_slot_release", "status": "unsupported",
             "detail": "Native Stop/result hooks cannot attest process-tree termination or reclaim a managed reservation."},
            {"name": "native_windows_hooks", "status": "unknown",
             "detail": "Source packages can generate py -3 hooks; Windows host and process-boundary integration tests are pending."},
        ],
        "permissions_changed": False,
        "authentication_attempted": False,
    }
