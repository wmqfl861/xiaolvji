"""Durable process launch, cancellation and conservative recovery.

Cooperative mode manages ordinary Linux descendants without restricting their
filesystem/network access. Controlled execution additionally requires a real
offline Linux sandbox and a durable, verified bootstrap attestation before any
task command can run. Missing capability or uncertain exit never falls back.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import uuid
from typing import Any, Awaitable, Callable

from .contracts import Fault, digest
from .supervisor import atomic_json, boot_id, process_identity


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SECRET_ENV = re.compile(r"(?:PARALLEL_DEV|XIAOLVJI|CONTROLLER|REVIEWER|PLANNER|INTEGRATOR|VERIFIER).*(?:TOKEN|KEY|SECRET|IDENTITY)|^(?:PYTHONPATH|PYTHONHOME|LD_PRELOAD|LD_LIBRARY_PATH)$", re.I)
# Closed allowlists, not provider prefixes: a host can use the same namespace
# for app/connector credentials or permission-broker capabilities. Those are
# not CLI provider credentials and must never cross the worker boundary.
_ENGINE_PREFIXES = ("OPENAI_", "CODEX_", "ANTHROPIC_", "ZCODE_", "ZAI_", "ZHIPU_", "AZURE_OPENAI_")
_ENGINE_ENV = {
    "CODEX_HOME", "CODEX_API_KEY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "OPENAI_API_VERSION",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_AD_TOKEN", "AZURE_OPENAI_DEPLOYMENT",
    "ZAI_API_KEY", "ZHIPU_API_KEY",
    # Explicit configuration keys in the pinned ZCode env-config adapter.
    "ZCODE_STORAGE_DIR", "ZCODE_SESSION_DB_PATH", "ZCODE_SESSION_DB", "ZCODE_HTTP_PROXY", "ZCODE_NO_PROXY",
    "ZCODE_AGENT_CA_CERT", "ZCODE_HTTP_TIMEOUT", "ZCODE_TIMEOUT", "ZCODE_LOG_FORMAT", "ZCODE_MAX_TOOL_CONCURRENCY",
}
_SERVICE_PREFIXES = ("CODEX_APPS_", "CODEX_CONNECTORS_", "OPENAI_EXECUTOR_")
_SERVICE_KEYS = {"CODEX_AUTH_PATH"}
_BASE_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TMP", "TEMP",
             "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
             "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
             "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_NUMERIC", "LC_TIME", "LC_COLLATE", "LC_MONETARY",
             "LC_MESSAGES", "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
             "XDG_CONFIG_DIRS", "XDG_DATA_DIRS"}


def _forbidden_environment_key(key: str) -> bool:
    normalized = key.upper()
    return bool(_SECRET_ENV.search(key) or normalized in _SERVICE_KEYS or
                normalized.startswith(_SERVICE_PREFIXES) or
                (normalized.startswith(_ENGINE_PREFIXES) and key not in _ENGINE_ENV))


def _child_environment(extra: dict[str, str] | None, attempt_id: str) -> dict[str, str]:
    # Preserve named CLI/provider configuration without inheriting an entire
    # host namespace. User configuration files remain available through HOME
    # and CODEX_HOME in cooperative mode; this does not add OS file isolation.
    env = {k: v for k, v in os.environ.items()
           if (k in _BASE_ENV or k in _ENGINE_ENV) and not _forbidden_environment_key(k)}
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value or "=" in key:
                raise Fault("INVALID_ARGUMENT", "Worker environment must contain valid string pairs")
            if _forbidden_environment_key(key):
                raise Fault("UNAUTHORIZED", "Host service credentials or unapproved engine variables cannot be passed to a worker")
            env[key] = value
    env["PARALLEL_DEV_ATTEMPT_ID"] = attempt_id
    env["PARALLEL_DEV_ROLE"] = "worker"
    return env


class ProcessExecutor:
    def __init__(self, state_dir: str | Path, profile: str = "cooperative_native"):
        if profile not in {"cooperative_native", "controlled_executor"}:
            raise Fault("INVALID_ARGUMENT", "Unknown execution profile")
        path = Path(state_dir).absolute()
        if path.is_symlink() or path.resolve() != path:
            raise Fault("INVALID_ARGUMENT", "Executor state must not contain symlink aliases")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir = path.resolve()
        self.launch_root = self.state_dir / "launches"
        self.launch_root.mkdir(mode=0o700, exist_ok=True)
        if self.launch_root.is_symlink():
            raise Fault("INVALID_ARGUMENT", "Executor launch directory must not be a symlink")
        self.profile = profile
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def probe(self) -> dict[str, Any]:
        reasons = []
        tracking = False
        if not sys.platform.startswith("linux") or not Path("/proc/self/stat").is_file():
            reasons.append("Linux /proc is required for the current process supervisor")
        elif not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            reasons.append("Identity-safe pidfd signalling is unavailable")
        else:
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                current = ctypes.c_int()
                tracking = libc.prctl(37, ctypes.byref(current), 0, 0, 0) == 0
                fd = os.pidfd_open(os.getpid(), 0)
                os.close(fd)
                boot_id()
                tracking = tracking and process_identity(os.getpid()) is not None
            except (OSError, AttributeError):
                tracking = False
            if not tracking:
                reasons.append("Linux child-subreaper or pidfd access is unavailable")
        sandbox = None
        if self.profile == "controlled_executor":
            from .sandbox import LinuxSandbox
            sandbox = LinuxSandbox(self.state_dir).probe()
            reasons.extend(sandbox.get("blocking_reasons", []))
        supported = tracking and (self.profile == "cooperative_native" or bool(sandbox and sandbox["supported"]))
        isolation = {"os_boundary": False, "status": "unsupported",
                     "limitations": ["Same-user workers are trusted in cooperative mode",
                                     "Filesystem and network access are not sandboxed",
                                     "A missing supervisor result never proves descendant exit"]}
        if sandbox is not None:
            isolation = {**sandbox, "os_boundary": supported, "status": "runtime_probed" if supported else "unsupported",
                         "limitations": ["This probe establishes the default offline boundary; explicit provider channels require their own attested launch",
                                         "Native host/model acceptance is not established by this OS capability probe",
                                         "CPU, memory and process-count resource quotas are not enforced",
                                         "A missing supervisor result remains uncertain even if a namespace disappears"]}
        return {"profile": self.profile, "supported": supported,
                "strict_supported": bool(supported and sandbox), "model_network_supported": False,
                "effective_mode": self.profile if supported else "blocked",
                "blocking_reasons": reasons,
                "process_tracking": {"backend": "linux_child_subreaper_v1", "status": "detected" if tracking else "unsupported"},
                "isolation": isolation}

    def _directory(self, attempt_id: str) -> Path:
        if not isinstance(attempt_id, str) or not _ID.fullmatch(attempt_id):
            raise Fault("INVALID_ARGUMENT", "Invalid attempt identity")
        return self.launch_root / attempt_id

    def _sandbox_preflight(self, working: Path, child_env: dict, policy: dict | None) -> tuple[str, dict]:
        from .sandbox import LinuxSandbox
        # Version probes and source scans can take time; run this read-only
        # preparation off the dispatch event loop so completed jobs can refill.
        with LinuxSandbox(self.state_dir).prepare(
                [sys.executable, "-I", "-S", "-c", "pass"], working, child_env, **(policy or {})) as launch:
            return launch.evidence["policy_digest"], dict(launch.env)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if path.is_symlink():
            raise Fault("INSTANCE_UNCERTAIN", "Launch state unexpectedly became a symlink")
        try:
            if path.stat().st_size > 1024 * 1024:
                raise Fault("INSTANCE_UNCERTAIN", "Launch record exceeds its size limit")
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise Fault("INSTANCE_UNCERTAIN", "Launch state could not be read safely") from exc

    async def run(self, attempt: dict[str, Any], argv: list[str], cwd: str,
                  env: dict[str, str] | None = None, stdin_text: str | None = None,
                  timeout: float = 3600,
                  on_started: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
                  sandbox_policy: dict[str, Any] | None = None,
                  provider_runtime: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt_id, generation = attempt.get("attempt_id"), attempt.get("generation")
        directory = self._directory(attempt_id)
        if directory.exists() or directory.is_symlink():
            raise Fault("INSTANCE_UNCERTAIN", "Launch journal exists; refusing a duplicate process")
        try:
            capabilities = await asyncio.to_thread(self.probe)
            if not capabilities["supported"]:
                raise Fault("CAPABILITY_UNSUPPORTED", "; ".join(capabilities["blocking_reasons"]))
            if type(generation) is not int or generation < 1:
                raise Fault("INVALID_ARGUMENT", "Attempt generation must be a positive integer")
            if not argv or not isinstance(argv, list) or any(not isinstance(x, str) or "\x00" in x for x in argv):
                raise Fault("INVALID_ARGUMENT", "Execution requires a nonempty argv array")
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise Fault("INVALID_ARGUMENT", "Execution timeout must be finite and positive")
            if stdin_text is not None and (not isinstance(stdin_text, str) or len(stdin_text.encode()) > 2 * 1024 * 1024):
                raise Fault("INVALID_ARGUMENT", "Worker stdin exceeds its size limit")
            working = Path(cwd).resolve(strict=True)
            if not working.is_dir() or working == self.state_dir or working in self.state_dir.parents:
                raise Fault("INVALID_ARGUMENT", "Worker cwd must not contain controller state")
            child_env = _child_environment(env, attempt_id)
            policy_digest = None
            provider_fds = ()
            if sandbox_policy is not None:
                if self.profile != "controlled_executor":
                    raise Fault("INVALID_ARGUMENT", "A sandbox policy requires the controlled execution profile")
                if not isinstance(sandbox_policy, dict) or set(sandbox_policy) - {"workspace", "write_scope", "readonly_paths", "writable_paths", "home_files", "provider_transport"}:
                    raise Fault("INVALID_ARGUMENT", "Sandbox policy contains unsupported fields")
            transport = (sandbox_policy or {}).get("provider_transport")
            if (transport is None) != (provider_runtime is None):
                raise Fault("INVALID_ARGUMENT", "An explicit provider channel and its runtime must be supplied together")
            if provider_runtime is not None:
                if self.profile != "controlled_executor":
                    raise Fault("INVALID_ARGUMENT", "Provider IPC is available only inside controlled execution")
                from .provider_bridge import validate_runtime
                provider_runtime = validate_runtime(provider_runtime)
                if not isinstance(transport, dict) or set(transport) != {"fd"} or type(transport["fd"]) is not int or transport["fd"] < 3:
                    raise Fault("INVALID_ARGUMENT", "Provider transport requires one explicit inherited descriptor")
                provider_fds = (transport["fd"],)
            if self.profile == "controlled_executor":
                # Preflight creates no worker. Validate the exact grants before the
                # journal/ledger handshake; the supervisor repeats this validation
                # and checks the same digest immediately before constructing them.
                if sandbox_policy is not None:
                    sandbox_policy = json.loads(json.dumps(sandbox_policy, allow_nan=False))
                policy_digest, child_env = await asyncio.to_thread(self._sandbox_preflight, working, child_env, sandbox_policy)
            nonce = uuid.uuid4().hex
            spec = {"nonce": nonce, "argv": argv, "cwd": str(working), "env": child_env,
                    "timeout": timeout, "stdin_text": stdin_text or "", "grace": 1.0,
                    "profile": self.profile, "sandbox_policy": sandbox_policy,
                    "sandbox_policy_digest": policy_digest, "provider_runtime": provider_runtime}
            encoded = (json.dumps(spec, separators=(",", ":")) + "\n").encode()
            if len(encoded) > 4 * 1024 * 1024:
                raise Fault("INVALID_ARGUMENT", "Launch request exceeds its size limit")
        except (Fault, OSError, ValueError, TypeError, UnicodeError) as exc:
            # A known preflight rejection releases a fresh reservation. Once a
            # journal exists, however, another launch may already own processes.
            if directory.exists() or directory.is_symlink():
                raise Fault("INSTANCE_UNCERTAIN", "A launch journal appeared during execution preflight") from exc
            details = {"no_process_created": True}
            if isinstance(exc, Fault):
                if isinstance(exc.details, dict):
                    details = {**exc.details, **details}
                elif exc.details is not None:
                    details["cause"] = exc.details
                raise Fault(exc.code, exc.message, details) from exc
            raise Fault("INVALID_ARGUMENT", "Execution preflight failed: " + type(exc).__name__, details) from exc
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise Fault("INSTANCE_UNCERTAIN", "Launch journal exists; refusing a duplicate process") from exc
        atomic_json(directory / "launch.json", {"attempt_id": attempt_id, "generation": generation,
                    "nonce": nonce, "launch_key": attempt.get("launch_key"), "profile": self.profile,
                    "sandbox_policy_digest": policy_digest})
        diagnostics = open(directory / "supervisor.log", "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).with_name("supervisor.py")),
                "--directory", str(directory), "--attempt", attempt_id,
                "--generation", str(generation), "--nonce", nonce, "--profile", self.profile,
                "--policy-digest", policy_digest or "",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=diagnostics, start_new_session=True, pass_fds=provider_fds,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        except OSError as exc:
            diagnostics.close()
            atomic_json(directory / "not-started.json", {"no_process_created": True, "errno": exc.errno})
            raise Fault("START_FAILED", "The owned supervisor could not be started", {"no_process_created": True}) from exc
        self._processes[attempt_id] = process
        identity = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                identity = self._read(directory / "identity.json")
                if identity:
                    break
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.01)
            if not identity:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    pass
                raise Fault("INSTANCE_UNCERTAIN", "Supervisor did not acknowledge a durable launch identity")
            if (identity.get("nonce") != nonce or identity.get("pid") != process.pid or
                    identity.get("profile") != self.profile or identity.get("sandbox_policy_digest") != policy_digest):
                process.stdin.close()
                raise Fault("INSTANCE_UNCERTAIN", "Supervisor launch identity did not match")
            if on_started:
                await on_started(identity)
            process.stdin.write(encoded)
            await process.stdin.drain()
            process.stdin.close()
            # The supervisor owns timeout and descendant cleanup. This wait is
            # asynchronous; unrelated tasks can finish and refill capacity.
            await process.wait()
            return self.reconcile(identity)
        except asyncio.CancelledError:
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
            if identity:
                await asyncio.shield(self.cancel(attempt_id, grace=0.2))
            raise
        except BaseException:
            # If recording the start failed, EOF leaves the supervisor unable
            # to launch the worker. Do not authorize execution in a finally.
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
            if identity:
                await self.cancel(attempt_id, grace=0.2)
            raise
        finally:
            diagnostics.close()
            if process.returncode is not None:
                self._processes.pop(attempt_id, None)

    def reconcile(self, identity: dict[str, Any]) -> dict[str, Any]:
        directory = self._directory(identity.get("attempt_id"))
        stored = self._read(directory / "identity.json")
        if stored != identity:
            raise Fault("UNKNOWN_IDENTITY", "Execution identity does not match its launch journal")
        launch = self._read(directory / "launch.json")
        # The durable process identity determines the boundary which must be
        # proven. A missing or mislabeled launch cannot downgrade strict
        # recovery, and shared-root trusted service launches remain cooperative.
        profile = identity.get("profile", "cooperative_native")
        if (not launch or launch.get("profile") != profile or
                any(launch.get(field) != identity.get(field) for field in ("attempt_id", "generation", "nonce")) or
                (profile == "controlled_executor" and
                 (not identity.get("sandbox_policy_digest") or
                  launch.get("sandbox_policy_digest") != identity.get("sandbox_policy_digest")))):
            raise Fault("INSTANCE_UNCERTAIN", "Durable launch metadata does not match its process identity")
        result = self._read(directory / "result.json")
        if result:
            if result.get("identity") != identity or type(result.get("process_tree_terminated")) is not bool:
                raise Fault("INSTANCE_UNCERTAIN", "Completion record is not bound to this process identity")
            if profile == "controlled_executor":
                if type(result.get("worker_started")) is not bool:
                    raise Fault("INSTANCE_UNCERTAIN", "Strict completion is missing command authorization evidence")
                if result["worker_started"]:
                    sandbox = self._read(directory / "sandbox.json")
                    if (not sandbox or result.get("sandbox") != sandbox or sandbox.get("attested") is not True or
                            digest(sandbox.get("policy")) != launch.get("sandbox_policy_digest") or
                            sandbox.get("policy_digest") != launch.get("sandbox_policy_digest") or
                            identity.get("sandbox_policy_digest") != launch.get("sandbox_policy_digest")):
                        raise Fault("INSTANCE_UNCERTAIN", "Strict completion is not bound to its durable sandbox attestation")
                    termination = result.get("termination_evidence", {})
                    if result["process_tree_terminated"] and not (
                            termination.get("namespace_init_exited") is True and termination.get("all_children_reaped") is True):
                        raise Fault("INSTANCE_UNCERTAIN", "Strict namespace termination is not proven")
            return {**result, "status": "exited" if result["process_tree_terminated"] else "uncertain"}
        same_namespace = os.readlink("/proc/self/ns/pid") == identity.get("pid_namespace")
        current = process_identity(identity.get("pid", -1)) if same_namespace else None
        same_boot = boot_id() == identity.get("boot_id")
        live = same_boot and current and current["proc_pid"] == identity.get("proc_pid") and current["start_time"] == identity.get("start_time") and current["state"] != "Z"
        return {"identity": identity, "status": "live" if live else "uncertain",
                "process_tree_terminated": False, "exit_code": None,
                "reason": "supervisor_running" if live else "no_durable_tree_exit_attestation"}

    async def cancel(self, attempt_id: str, grace: float = 2.0) -> dict[str, Any]:
        directory = self._directory(attempt_id)
        identity = self._read(directory / "identity.json")
        if not identity:
            return {"status": "uncertain", "process_tree_terminated": False,
                    "reason": "no_durable_launch_identity"}
        current = self.reconcile(identity)
        if current["status"] != "live":
            return current
        if not isinstance(grace, (int, float)) or not math.isfinite(grace) or not 0 <= grace <= 30:
            raise Fault("INVALID_ARGUMENT", "Cancellation grace must be between zero and 30 seconds")
        request = directory / "cancel.json"
        if not request.exists():
            atomic_json(request, {"nonce": identity["nonce"], "grace": grace})
        deadline = time.monotonic() + grace + 12
        while time.monotonic() < deadline:
            current = self.reconcile(identity)
            if current["status"] != "live":
                return current
            await asyncio.sleep(0.025)
        return {"identity": identity, "status": "uncertain", "process_tree_terminated": False,
                "exit_code": None, "reason": "cancellation_not_confirmed"}
