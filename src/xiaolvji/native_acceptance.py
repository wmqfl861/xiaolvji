"""Credential-free native-host acceptance inside the actual strict boundary.

This is a fixed local protocol fixture, not a real model or provider benchmark.
No JSON or environment setting can select this fixture in production transport.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shlex
import socket
import sys
import time

from .authority import atomic_json
from .contracts import Fault, canonical_json, digest
from .executor import ProcessExecutor
from .host_config import derive_request_constraints, normalize_host_snapshot, render_host_config
from .native_result import verify_host_output
from .provider_broker import ProviderBroker, normalize_provider_profile
from .provider_bridge import BRIDGE_URL
from .workspace import git

ASSERTIONS = frozenset({"native_process_succeeded", "complete_native_turn", "strict_boundary_attested",
    "process_tree_terminated", "exact_model_and_quality", "native_delegation_absent",
    "two_model_requests", "tool_result_returned", "scoped_write_succeeded",
    "sibling_write_denied", "git_write_denied", "external_network_denied", "controller_file_hidden"})


def runner_source():
    return Path(__file__).with_name("codex_app_server.py").read_text(encoding="utf-8")


def native_readonly_paths(executable):
    # Keep one mount authority. A canonical binary already visible through an
    # implicit runtime grant must not be added as an overlapping explicit grant.
    from .sandbox import readable_host_sources
    path = Path(executable)
    return [] if any(path == source or path.is_relative_to(source)
                     for source in readable_host_sources()) else [str(path)]


def native_command(deployment, snapshot, workspace, prompt, argv_extra):
    """Only the controller uses this fixed runner in an attested strict job."""
    config = deployment.value
    if config["engine"] != "codex" or config["protocol"] != "codex-app-server-v2":
        raise Fault("CAPABILITY_UNSUPPORTED", "This native runtime has no verified execution adapter.")
    envelope = {"version": 1, "executable": config["executable"], "cwd": str(workspace),
                "prompt": prompt, "model": snapshot["model"],
                "effort": snapshot["quality"]["model_reasoning_effort"], "argv_extra": argv_extra}
    return {"argv": [sys.executable, "-I", "-S", "/home/xiaolvji/.xiaolvji/codex_app_server.py"],
            "stdin_text": canonical_json(envelope), "env": {}}


class _Fixture:
    def __init__(self, model, command):
        self.model, self.command = model, command
        self.requests = []
        self.errors = []
        self.tasks, self.connections = set(), set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0, family=socket.AF_INET, limit=32768)
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections.add(writer)
        try:
            async with asyncio.timeout(15):
                head = await reader.readuntil(b"\r\n\r\n")
                if len(head) > 32768:
                    raise ValueError()
                lines = head.decode("ascii").split("\r\n")
                headers = {key.lower(): value.strip() for key, value in
                           (line.split(":", 1) for line in lines[1:] if ":" in line)}
                length = int(headers["content-length"])
                if not 0 < length <= 16 * 1024 * 1024 or lines[0] != "POST /v1/responses HTTP/1.1":
                    raise ValueError()
                body = json.loads(await reader.readexactly(length))
                self.requests.append(body)
                index = len(self.requests)
                if index > 2:
                    raise ValueError()
                if index == 1:
                    item = {"type": "function_call", "id": "fc_native_probe", "call_id": "call_native_probe",
                            "name": "exec_command", "status": "completed",
                            "arguments": json.dumps({"cmd": self.command, "login": False,
                                                     "yield_time_ms": 1000, "max_output_tokens": 1000})}
                else:
                    item = {"type": "message", "id": "msg_native_probe", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "Native protocol fixture completed.", "annotations": []}]}
                response = {"id": "response_native_probe_" + str(index), "object": "response", "status": "completed",
                            "model": self.model, "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": response}]
                payload = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: " +
                             str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
        except (OSError, ValueError, KeyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            self.errors.append("synthetic_response_failed")
        finally:
            writer.close()
            self.connections.discard(writer)
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for connection in list(self.connections):
            connection.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def verify_native_host(deployment, snapshot, output):
    """Exercise the exact supplied model configuration without its credentials."""
    snapshot = normalize_host_snapshot(snapshot)
    if deployment.value["engine"] != snapshot["engine"] or snapshot["engine"] != "codex":
        raise Fault("CAPABILITY_UNSUPPORTED", "A complete native adapter is available only for the verified Codex profile.")
    root = Path(output).absolute()
    if root.resolve() != root or root.exists():
        raise Fault("DESTINATION_EXISTS", "Native acceptance requires a fresh non-symlink output directory.")
    root.mkdir(parents=True, mode=0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "owned").mkdir()
    (workspace / "sibling.txt").write_text("protected sibling\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("Run only the fixed local acceptance probe. No delegation.\n", encoding="utf-8")
    git("init", cwd=workspace)
    canary = root / "controller-only.txt"
    canary.write_text("synthetic controller canary\n", encoding="utf-8")
    script = "\n".join([
        "from pathlib import Path", "import json,socket", "r={'native_boundary_probe':True}",
        "Path('owned/allowed.txt').write_text('scoped native write')", "r['scoped_write_succeeded']=True",
        "for name,path in [('sibling_write_denied','sibling.txt'),('git_write_denied','.git/native-probe')]:",
        " try: Path(path).write_text('unexpected');r[name]=False", " except OSError:r[name]=True",
        "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(.2)",
        "try:s.connect(('192.0.2.1',443));r['external_network_denied']=False",
        "except OSError:r['external_network_denied']=True", "s.close()",
        "try:Path(" + repr(str(canary)) + ").read_text();r['controller_file_hidden']=False",
        "except OSError:r['controller_file_hidden']=True",
        "Path('owned/probe.json').write_text(json.dumps(r))", "print(json.dumps(r))",
        "assert all(r.values())",
    ])
    fixture = _Fixture(snapshot["model"], shlex.join([sys.executable, "-I", "-c", script]))
    port = await fixture.start()
    class FixtureBroker(ProviderBroker):
        async def _connect_upstream(self, profile):
            return await asyncio.open_connection("127.0.0.1", port, family=socket.AF_INET, limit=32768)
    constraints = derive_request_constraints(snapshot)
    constraints.pop("model")
    profile = normalize_provider_profile({"profile_id": snapshot["provider_id"], "api": snapshot["api"],
        "model": snapshot["model"], "upstream_url": "https://native-fixture.invalid/v1/responses",
        "request_constraints": constraints, "max_requests": 4, "max_concurrency": 1, "request_timeout": 20})
    phase = "starting"
    attempt = {"attempt_id": "native-probe", "generation": 1, "launch_key": "native-probe:1"}
    def admitted(binding, requested):
        return (all(binding.get(k) == v for k, v in attempt.items()) and
                phase == ("starting" if requested == "grant" else "running"))
    broker = FixtureBroker(root / "broker", {profile.profile_id: profile},
                          {profile.profile_id: "synthetic-native-probe-credential"}, admitted)
    grant = None
    started = time.monotonic()
    try:
        await broker.start()
        grant = await broker.grant(attempt, profile.profile_id, ttl_seconds=65)
        rendered = render_host_config(snapshot, BRIDGE_URL, grant.capability)
        runtime = {"version": 1, "capability": grant.capability, "api": snapshot["api"], "env": rendered["env"],
                   "max_request_bytes": profile.max_request_bytes, "request_timeout": profile.request_timeout}
        files = [{"path": item["relative_path"], "content": item["content"]} for item in rendered["files"]]
        files.append({"path": ".xiaolvji/codex_app_server.py", "content": runner_source()})
        command = native_command(deployment, snapshot, workspace,
            "Run the fixed local boundary probe supplied by the synthetic fixture and report its result.", rendered["argv_extra"])
        async def on_started(identity):
            nonlocal phase
            phase = "running"
            grant.close_worker_endpoint()
        executor = ProcessExecutor(root / "executor", profile="controlled_executor")
        result = await executor.run(attempt, command["argv"], str(workspace), stdin_text=command["stdin_text"],
            timeout=50, on_started=on_started, provider_runtime=runtime,
            sandbox_policy={"workspace": str(workspace), "write_scope": [{"path": "owned", "kind": "directory"}],
                "readonly_paths": native_readonly_paths(deployment.value["executable"]), "writable_paths": [],
                "provider_transport": {"fd": grant.worker_socket.fileno()}, "home_files": files})
        report_error = None
        semantic = None
        try:
            semantic = verify_host_output("codex", result.get("stdout_path"),
                                         truncated=(result.get("log_truncated") or {}).get("stdout"))
        except Fault as exc:
            report_error = exc.code
        observed = {}
        probe_file = workspace / "owned/probe.json"
        if probe_file.is_file() and not probe_file.is_symlink() and probe_file.stat().st_size < 4096:
            try:
                observed = json.loads(probe_file.read_text())
            except (ValueError, UnicodeError):
                pass
        tool_names = {t.get("name", "") for request in fixture.requests for t in request.get("tools", []) if isinstance(t, dict)}
        forbidden = {"spawn_agent", "send_message", "wait_agent", "resume_agent", "close_agent", "agent", "task"}
        assertions = {name: False for name in ASSERTIONS}
        assertions.update({
            "native_process_succeeded": result.get("exit_code") == 0 and result.get("timed_out") is False and result.get("cancelled") is False,
            "complete_native_turn": semantic is not None,
            "strict_boundary_attested": (result.get("sandbox") or {}).get("attested") is True,
            "process_tree_terminated": result.get("process_tree_terminated") is True,
            "exact_model_and_quality": len(fixture.requests) > 0 and all(r.get("model") == snapshot["model"] for r in fixture.requests),
            "native_delegation_absent": bool(tool_names) and not forbidden.intersection(name.casefold() for name in tool_names),
            "two_model_requests": len(fixture.requests) == 2 and not fixture.errors,
            "tool_result_returned": len(fixture.requests) == 2 and "native_boundary_probe" in json.dumps(fixture.requests[1].get("input")),
            "scoped_write_succeeded": observed.get("scoped_write_succeeded") is True and (workspace / "owned/allowed.txt").read_text() == "scoped native write" if (workspace / "owned/allowed.txt").is_file() else False,
            "sibling_write_denied": observed.get("sibling_write_denied") is True and (workspace / "sibling.txt").read_text() == "protected sibling\n",
            "git_write_denied": observed.get("git_write_denied") is True and not (workspace / ".git/native-probe").exists(),
            "external_network_denied": observed.get("external_network_denied") is True,
            "controller_file_hidden": observed.get("controller_file_hidden") is True,
        })
        report = {"schema_version": 1, "status": "passed" if all(assertions.values()) else "failed",
                  "kind": "native_protocol_fixture", "engine": "codex", "protocol": "codex-app-server-v2",
                  "assertions": assertions, "host_snapshot_digest": digest(snapshot), "deployment_digest": deployment.digest,
                  "native_host_invocations": 1, "synthetic_provider_requests": len(fixture.requests), "real_provider_requests": 0,
                  "authentication_attempted": False, "native_result": semantic, "semantic_error": report_error,
                  "executor_identity": result.get("identity"), "sandbox_policy_digest": (result.get("sandbox") or {}).get("policy_digest"),
                  "elapsed_seconds": round(time.monotonic() - started, 3)}
        atomic_json(root / "report.json", report)
        return report
    finally:
        phase = "closed"
        if grant is not None:
            grant.close_worker_endpoint()
        await broker.close()
        await fixture.close()
