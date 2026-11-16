"""Bind provider grants to the authoritative task lifecycle.

Native-host readiness remains an independent gate in Pipeline/doctor. This
runtime is also used by deterministic strict-execution fixtures, which exercise
real IPC, namespaces and HTTP without implying a real model integration.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .contracts import Fault, canonical_json, digest, load_json
from .host_config import render_host_config
from .provider_bridge import BRIDGE_URL, validate_runtime
from .provider_broker import ProviderBroker
from .provider_config import ProviderConfiguration, read_explicit_credentials


class ProviderRuntime:
    def __init__(self, state_dir, ledger, configuration: ProviderConfiguration, *,
                 broker_factory=ProviderBroker, credentials=None):
        if not isinstance(configuration, ProviderConfiguration):
            raise Fault("INVALID_PROVIDER_CONFIG", "A validated explicit provider configuration is required.")
        self.root = Path(state_dir)
        self.ledger = ledger
        self.configuration = configuration
        self._factory = broker_factory
        # Programmatic callers may supply their own explicit secret mapping.
        # CLI callers always use protected, operator-named files instead.
        self._explicit_credentials = dict(credentials) if credentials is not None else None
        self.broker = None
        self._grants = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def _admitted(self, binding, phase):
        current = self.ledger.attempt(binding["attempt_id"])
        if (any(current.get(key) != binding[key] for key in ("attempt_id", "generation", "launch_key")) or
                current["cancel_requested"] or not current["project"]["enabled"] or
                current["project"]["config"]["profile"] != "controlled_executor"):
            return False
        self.configuration.host(current["project_id"], current["task"]["engine"])
        if phase == "grant":
            return current["state"] == "STARTING" and current["launch_state"] == "CLAIMED"
        return phase == "request" and current["state"] == "RUNNING" and current.get("identity") is not None

    async def start(self):
        async with self._lock:
            if self._closed:
                raise Fault("BROKER_STATE_ERROR", "Provider runtime has already closed.")
            if self.broker is not None:
                return
            credentials = self._explicit_credentials
            if credentials is None:
                roots = [p["root"] for p in self.ledger.status()["projects"]]
                roots.append(self.root.parent / "workspaces")
                credentials = await asyncio.to_thread(read_explicit_credentials, self.configuration, forbidden_roots=roots)
            try:
                broker = self._factory(self.root, self.configuration.profiles(), credentials, self._admitted,
                                       global_concurrency=self.configuration.value["global_request_limit"])
                await broker.start()
            finally:
                credentials.clear()
                self._explicit_credentials = None
            self.broker = broker

    async def bind(self, attempt, command, timeout, *, native=True):
        current = self.ledger.attempt(attempt["attempt_id"])
        if (any(current.get(key) != attempt.get(key) for key in ("attempt_id", "generation", "launch_key", "project_id", "task_id")) or
                current["task"]["engine"] != attempt.get("task", {}).get("engine")):
            raise Fault("BROKER_ADMISSION_DENIED", "Provider launch envelope differs from its authoritative attempt.")
        attempt = current
        host = self.configuration.host(current["project_id"], current["task"]["engine"])
        if current["project"]["config"]["profile"] != "controlled_executor":
            raise Fault("CAPABILITY_UNSUPPORTED", "Scoped provider transport requires controlled execution.")
        await self.start()
        grant = await self.broker.grant(attempt, host["provider_id"], ttl_seconds=min(86400, timeout + 30))
        if self._closed:
            grant.close_worker_endpoint()
            await self.broker.revoke(grant.binding["attempt_id"], grant.binding["generation"])
            raise Fault("BROKER_STATE_ERROR", "Provider runtime closed during grant creation.")
        key = (attempt["attempt_id"], attempt["generation"])
        self._grants[key] = grant
        try:
            rendered = render_host_config(host, BRIDGE_URL, grant.capability)
            profile = self.configuration.profiles()[host["provider_id"]]
            runtime = validate_runtime({"version": 1, "api": profile.api, "capability": grant.capability,
                "env": rendered["env"], "max_request_bytes": profile.max_request_bytes,
                "request_timeout": profile.request_timeout})
            argv = list(command["argv"])
            stdin_text = command.get("stdin_text")
            if native:
                if command.get("native_protocol") == "codex-app-server-v2" and host["engine"] == "codex":
                    if not isinstance(stdin_text, str):
                        raise Fault("CAPABILITY_UNSUPPORTED", "Native runner requires a JSON-text launch envelope.")
                    envelope = load_json(stdin_text)
                    if (not isinstance(envelope, dict) or envelope.get("model") != host["model"] or
                            envelope.get("effort") != host["quality"]["model_reasoning_effort"] or
                            envelope.get("argv_extra") != []):
                        raise Fault("CAPABILITY_UNSUPPORTED", "Native runner does not bind the exact approved host snapshot.")
                    envelope["argv_extra"] = rendered["argv_extra"]
                    stdin_text = canonical_json(envelope)
                elif host["engine"] == "codex":
                    if argv[-1] != "-":
                        raise Fault("CAPABILITY_UNSUPPORTED", "Codex launch does not match the verified adapter contract.")
                    argv = argv[:-1] + rendered["argv_extra"] + argv[-1:]
                else:
                    argv.extend(rendered["argv_extra"])
            return {"command": {**command, "argv": argv, "stdin_text": stdin_text}, "provider_runtime": runtime,
                    "sandbox_additions": {"provider_transport": {"fd": grant.worker_socket.fileno()},
                        "home_files": [{"path": item["relative_path"], "content": item["content"]} for item in rendered["files"]]},
                    "provider_binding": {"schema_version": 1, "binding": dict(grant.binding),
                        "broker_policy_digest": grant.policy_digest, "configuration_digest": self.configuration.digest,
                        "host_snapshot_digest": rendered["snapshot_digest"], "host_config_digest": rendered["config_digest"],
                        "provider_id": host["provider_id"], "model": host["model"],
                        "quality_digest": digest(host["quality"]), "host_evidence": rendered["evidence"]}}
        except BaseException:
            await self.release(attempt)
            raise

    def handed_off(self, attempt):
        grant = self._grants.get((attempt["attempt_id"], attempt["generation"]))
        if grant is not None:
            grant.close_worker_endpoint()

    async def release(self, attempt):
        key = (attempt["attempt_id"], attempt["generation"])
        grant = self._grants.pop(key, None)
        if grant is not None:
            grant.close_worker_endpoint()
            await self.broker.revoke(*key)

    async def close(self):
        async with self._lock:
            self._closed = True
            for grant in self._grants.values():
                grant.close_worker_endpoint()
            self._grants.clear()
            if self.broker is not None:
                await self.broker.close()
            if self._explicit_credentials is not None:
                self._explicit_credentials.clear()
                self._explicit_credentials = None
