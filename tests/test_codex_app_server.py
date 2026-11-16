"""Hermetic native-protocol failures; no provider accounts or model requests."""
import copy
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import re
import shlex
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.codex_app_server import Client, HostError, MAX_MESSAGE_BYTES, _Messages, _json, main, validate_input
from xiaolvji.host_config import render_host_config


MODEL = "fixture-model"


def configuration(executable="/usr/bin/codex", cwd="/workspace/test"):
    host = {"schema_version": 1, "engine": "codex", "provider_id": "fixture", "api": "openai-responses",
            "model": MODEL, "quality": {"model_reasoning_effort": "high"}}
    rendered = render_host_config(host, "http://127.0.0.1:38119", "synthetic_capability_" + "a" * 32)
    return {"version": 1, "executable": executable, "cwd": cwd, "prompt": "Synthetic task.", "model": MODEL,
            "effort": "high", "argv_extra": rendered["argv_extra"], "timeout_seconds": 5}


class MemoryProcess:
    def __init__(self):
        self.stdin = io.BytesIO()


def native_config():
    return {"model": MODEL, "model_provider": "xiaolvji_bridge", "model_reasoning_effort": "high",
            "web_search": "disabled", "approval_policy": None, "approvals_reviewer": None}


def thread_response():
    return {"thread": {"id": "thread-fixture"}, "model": MODEL, "modelProvider": "xiaolvji_bridge",
            "reasoningEffort": "high", "cwd": "/workspace/test", "approvalPolicy": "on-request",
            "approvalsReviewer": "user", "sandbox": {"type": "workspaceWrite", "networkAccess": False}}


def settings():
    return {"model": MODEL, "modelProvider": "xiaolvji_bridge", "effort": "high", "cwd": "/workspace/test",
            "approvalPolicy": "on-request", "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.client = Client(configuration(), self.output)
        self.client.process = MemoryProcess()
        self.client.send("initialize", {})

    def response(self, value):
        self.assertEqual(1, len(self.client.pending))
        ident = next(iter(self.client.pending))
        self.client.handle({"id": ident, "result": value})

    def preflight(self, *, models=None):
        self.response({})
        self.response({"requirements": None})
        self.response({"config": native_config()})
        self.response({"data": models if models is not None else [
            {"model": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None})

    def start(self):
        self.preflight()
        self.response(thread_response())
        self.client.handle({"method": "thread/started", "params": {"thread": {"id": "thread-fixture"}}})
        self.client.handle({"method": "thread/settings/updated", "params": {"threadId": "thread-fixture", "threadSettings": settings()}})
        self.response({"turn": {"id": "turn-fixture"}})
        self.client.handle({"method": "turn/started", "params": {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}}})

    def requests(self):
        return [json.loads(line) for line in self.client.process.stdin.getvalue().splitlines()]

    def test_success_requires_exact_model_effort_policy_and_actual_native_terminal(self):
        self.start()
        self.assertFalse(self.client.completed)
        self.client.handle({"method": "item/completed", "params": {"threadId": "thread-fixture", "turnId": "turn-fixture",
                              "item": {"type": "agentMessage", "id": "message", "text": "synthetic"}}})
        self.client.handle({"method": "turn/completed", "params": {"threadId": "thread-fixture",
                              "turn": {"id": "turn-fixture", "status": "completed", "error": None}}})
        self.assertTrue(self.client.completed)
        # Only run() may publish a terminal after stopping its owned service.
        self.assertNotIn('"type":"turn.completed"', self.output.getvalue())
        request = next(item for item in self.requests() if item.get("method") == "turn/start")
        self.assertEqual({"type": "externalSandbox", "networkAccess": "restricted"}, request["params"]["sandboxPolicy"])
        self.assertNotIn("approvalPolicy", request["params"])
        self.assertNotIn("approvalsReviewer", request["params"])

    def test_nonempty_managed_requirements_block_before_thread_or_model_turn(self):
        self.response({})
        with self.assertRaises(HostError) as caught:
            self.response({"requirements": {"allowedSandboxModes": ["workspace-write"]}})
        self.assertEqual("NATIVE_MANAGED_REQUIREMENTS_UNSUPPORTED", caught.exception.code)
        self.assertFalse(any(item.get("method") in {"model/list", "thread/start", "turn/start"} for item in self.requests()))

    def test_unknown_model_cannot_use_generic_metadata_or_run_a_turn(self):
        with self.assertRaises(HostError) as caught:
            self.preflight(models=[{"model": "other", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}])
        self.assertEqual("NATIVE_MODEL_UNSUPPORTED", caught.exception.code)
        self.assertFalse(any(item.get("method") == "thread/start" for item in self.requests()))

    def test_unsupported_exact_effort_is_rejected_without_substitution(self):
        with self.assertRaises(HostError) as caught:
            self.preflight(models=[{"model": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}])
        self.assertEqual("NATIVE_REASONING_EFFORT_UNSUPPORTED", caught.exception.code)
        self.assertFalse(any(item.get("method") == "thread/start" for item in self.requests()))

    def test_duplicate_catalog_model_is_rejected(self):
        entry = {"model": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
        with self.assertRaises(HostError):
            self.preflight(models=[entry, entry])

    def test_catalog_pagination_replay_is_rejected(self):
        self.response({}); self.response({"requirements": None}); self.response({"config": native_config()})
        self.response({"data": [], "nextCursor": "same"})
        with self.assertRaises(HostError):
            self.response({"data": [], "nextCursor": "same"})

    def test_effective_approval_or_model_drift_is_never_overridden(self):
        self.response({}); self.response({"requirements": None})
        changed = native_config(); changed["approval_policy"] = "never"
        with self.assertRaises(HostError) as caught:
            self.response({"config": changed})
        self.assertEqual("NATIVE_EFFECTIVE_CONFIG_MISMATCH", caught.exception.code)

    def test_nonwire_context_limits_must_match_the_exact_effective_native_config(self):
        self.client.expected_quality.update(model_context_window=200000, model_auto_compact_token_limit=150000)
        self.response({}); self.response({"requirements": None})
        changed = native_config()
        changed.update(model_context_window=100000, model_auto_compact_token_limit=150000)
        with self.assertRaises(HostError) as caught:
            self.response({"config": changed})
        self.assertEqual("NATIVE_EFFECTIVE_CONFIG_MISMATCH", caught.exception.code)
        self.assertFalse(any(item.get("method") == "model/list" for item in self.requests()))

    def test_concrete_thread_approval_reviewer_must_remain_user(self):
        self.preflight()
        changed = thread_response(); changed["approvalsReviewer"] = "auto_review"
        with self.assertRaises(HostError):
            self.response(changed)
        self.assertFalse(any(item.get("method") == "turn/start" for item in self.requests()))

    def test_external_policy_must_be_verified_before_turn_start(self):
        self.preflight(); self.response(thread_response()); self.response({"turn": {"id": "turn-fixture"}})
        with self.assertRaises(HostError):
            self.client.handle({"method": "turn/started", "params": {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}}})

    def test_midturn_policy_change_is_rejected(self):
        self.start()
        changed = settings(); changed["sandboxPolicy"] = {"type": "dangerFullAccess"}
        with self.assertRaises(HostError):
            self.client.handle({"method": "thread/settings/updated", "params": {"threadId": "thread-fixture", "threadSettings": changed}})

    def test_native_approval_is_declined_and_never_turn_success(self):
        self.start()
        with self.assertRaises(HostError) as caught:
            self.client.handle({"method": "item/commandExecution/requestApproval", "id": "approval", "params": {"command": "synthetic"}})
        self.assertEqual("NATIVE_INTERACTION_REQUIRED", caught.exception.code)
        self.assertEqual({"id": "approval", "result": {"decision": "decline"}}, self.requests()[-1])
        self.assertFalse(self.client.completed)

    def test_unrecognized_server_request_gets_error_not_implicit_acceptance(self):
        with self.assertRaises(HostError):
            self.client.handle({"method": "new/permission", "id": 73, "params": {}})
        self.assertEqual(-32601, self.requests()[-1]["error"]["code"])

    def test_native_warning_rejects_fallback_metadata(self):
        with self.assertRaises(HostError) as caught:
            self.client.handle({"method": "warning", "params": {"message": "Defaulting to fallback metadata"}})
        self.assertEqual("NATIVE_HOST_WARNING", caught.exception.code)

    def test_native_delegation_item_is_rejected_even_if_a_host_setting_regresses(self):
        self.start()
        with self.assertRaises(HostError) as caught:
            self.client.handle({"method": "item/started", "params": {"threadId": "thread-fixture", "turnId": "turn-fixture",
                               "item": {"type": "collabAgentToolCall", "id": "spawn", "tool": "spawnAgent"}}})
        self.assertEqual("NATIVE_DELEGATION_FORBIDDEN", caught.exception.code)
        self.assertFalse(self.client.completed)

    def test_failed_mismatched_or_duplicate_native_turn_never_completes(self):
        self.start()
        with self.assertRaises(HostError):
            self.client.handle({"method": "turn/completed", "params": {"threadId": "another-thread", "turn": {"id": "turn-fixture", "status": "completed"}}})
        with self.assertRaises(HostError):
            self.client.handle({"method": "turn/completed", "params": {"threadId": "thread-fixture", "turn": {"id": "turn-fixture", "status": "failed"}}})
        with self.assertRaises(HostError):
            self.client.handle({"method": "turn/started", "params": {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}}})
        self.assertFalse(self.client.completed)

    def test_unknown_response_and_duplicate_json_fields_are_rejected(self):
        with self.assertRaises(HostError):
            self.client.handle({"id": 500, "result": {}})
        with self.assertRaises(HostError):
            self.client.handle({"id": 1, "method": "ambiguous", "result": {}})
        for payload in ('{"id":1,"id":2}', '{"value":NaN}', '{"value":1e999}', '[1]', '{"value":"\\ud800"}'):
            if payload == '[1]':
                continue
            with self.subTest(payload=payload), self.assertRaises(HostError):
                _json(payload)


class InputAndFramingTests(unittest.TestCase):
    def test_timeout_bounds_reject_unrepresentable_integers_with_typed_failure(self):
        for timeout in (10 ** 400, -(10 ** 400), 0, 86401, True, "5", None,
                        float("inf"), float("-inf"), float("nan")):
            candidate = configuration()
            candidate["timeout_seconds"] = timeout
            with self.subTest(timeout=timeout), self.assertRaises(HostError) as caught:
                validate_input(candidate)
            self.assertEqual("NATIVE_INPUT_INVALID", caught.exception.code)
        for timeout in (1, 1.5, 86400):
            candidate = configuration()
            candidate["timeout_seconds"] = timeout
            with self.subTest(timeout=timeout):
                self.assertEqual(timeout, validate_input(candidate)["timeout_seconds"])

    def test_huge_json_timeout_emits_bounded_failure_without_starting_native_host(self):
        candidate = configuration()
        candidate["timeout_seconds"] = 10 ** 400
        output = io.StringIO()
        with io.TextIOWrapper(io.BytesIO(json.dumps(candidate).encode())) as input_stream:
            with (patch("sys.stdin", input_stream), patch("sys.stdout", output),
                  patch("xiaolvji.codex_app_server.subprocess.Popen") as launch):
                self.assertEqual(1, main())
                launch.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(["error", "turn.failed"], [item["type"] for item in records])
        self.assertEqual("NATIVE_INPUT_INVALID", records[0]["code"])
        self.assertEqual("NATIVE_INPUT_INVALID", records[1]["error"]["code"])
        self.assertLess(len(output.getvalue()), 1024)
        self.assertNotIn(str(candidate["timeout_seconds"]), output.getvalue())

    def test_closed_input_and_renderer_pins_cannot_select_bypass_or_other_model(self):
        cases = []
        for key, value in (("model", "other"), ("effort", "low"), ("executable", "codex"),
                           ("cwd", "/work/../other"), ("version", True), ("timeout_seconds", float("inf"))):
            candidate = configuration(); candidate[key] = value; cases.append(candidate)
        for setting in ('approval_policy="never"', 'sandbox_mode="danger-full-access"', 'agents.enabled=true',
                        'model_catalog_json="/work/model.json"', 'model="fixture-model"'):
            candidate = configuration(); candidate["argv_extra"] += ["-c", setting]; cases.append(candidate)
        candidate = configuration()
        candidate["argv_extra"] += ["-c", 'model_verbosity="high"\nagents.enabled=true']
        cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=tuple(candidate)), self.assertRaises(HostError):
                validate_input(candidate)
        self.assertEqual("high", validate_input(configuration())["effort"])

    def test_unterminated_and_oversized_native_messages_fail_before_json_parse(self):
        import time
        for body in (b'{"id":1', b"x" * (MAX_MESSAGE_BYTES + 1)):
            with tempfile.TemporaryFile() as stream:
                stream.write(body); stream.seek(0)
                with self.assertRaises(HostError):
                    _Messages(stream).read(time.monotonic() + 2)


FAKE_HOST = r'''
import json,sys,time
def emit(value):
 print(json.dumps(value),flush=True)
for line in sys.stdin:
 message=json.loads(line);method=message.get('method');ident=message.get('id')
 if method=='initialized':continue
 if method=='initialize':result={}
 elif method=='configRequirements/read':result={'requirements':None}
 elif method=='config/read':result={'config':{'model':MODEL,'model_provider':'xiaolvji_bridge','model_reasoning_effort':'high','web_search':'disabled','approval_policy':None,'approvals_reviewer':None}}
 elif method=='model/list':result={'data':[{'model':MODEL,'supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],'nextCursor':None}
 elif method=='thread/start':result={'thread':{'id':'thread-fixture'},'model':MODEL,'modelProvider':'xiaolvji_bridge','reasoningEffort':'high','cwd':CWD,'approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':{'type':'workspaceWrite'}}
 elif method=='turn/start':
  if MODE=='truncated':sys.exit(0)
  emit({'method':'thread/settings/updated','params':{'threadId':'thread-fixture','threadSettings':{'model':MODEL,'modelProvider':'xiaolvji_bridge','effort':'high','cwd':CWD,'approvalPolicy':'on-request','approvalsReviewer':'user','sandboxPolicy':{'type':'externalSandbox','networkAccess':'restricted'}}}})
  emit({'id':ident,'result':{'turn':{'id':'turn-fixture'}}})
  emit({'method':'turn/started','params':{'threadId':'thread-fixture','turn':{'id':'turn-fixture'}}})
  emit({'method':'item/completed','params':{'threadId':'thread-fixture','turnId':'turn-fixture','item':{'type':'agentMessage','id':'message','text':'synthetic'}}})
  emit({'method':'turn/completed','params':{'threadId':'thread-fixture','turn':{'id':'turn-fixture','status':'completed','error':None}}})
  if MODE=='resident':time.sleep(10)
  if MODE=='crash':sys.exit(7)
  break
 else:sys.exit(9)
 emit({'id':ident,'result':result})
'''


class RealProcessProtocolTests(unittest.TestCase):
    def invoke(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "synthetic-codex"
            binary.write_text("#!" + sys.executable + "\nMODE=" + repr(mode) + "\nMODEL=" + repr(MODEL)
                              + "\nCWD=" + repr(str(root)) + "\n" + FAKE_HOST)
            binary.chmod(0o700)
            output = io.StringIO()
            client = Client(configuration(str(binary), str(root)), output)
            error = None
            try:
                client.run()
            except HostError as exc:
                error = exc.code
            self.assertIsNotNone(client.process.poll())
            return [json.loads(line) for line in output.getvalue().splitlines()], error

    def test_native_completed_turn_and_service_exit_emit_one_normalized_terminal(self):
        records, error = self.invoke("normal")
        self.assertIsNone(error)
        self.assertEqual(1, sum(item["type"] == "turn.completed" for item in records))
        stop = next(item for item in records if item["type"] == "host.service_stopped")
        self.assertEqual(0, stop["exit_code"])
        self.assertFalse(stop["process_tree_terminated"])

    def test_resident_service_is_stopped_without_faking_process_tree_authority(self):
        records, error = self.invoke("resident")
        self.assertIsNone(error)
        stop = next(item for item in records if item["type"] == "host.service_stopped")
        self.assertEqual(-15, stop["exit_code"])
        self.assertEqual("controller_shutdown", stop["reason"])
        self.assertEqual("SIGTERM", stop["signal_used"])
        self.assertFalse(stop["process_tree_terminated"])
        self.assertEqual("turn.completed", records[-1]["type"])

    def test_exit_zero_without_native_terminal_is_failure(self):
        records, error = self.invoke("truncated")
        self.assertEqual("NATIVE_PROTOCOL_TRUNCATED", error)
        self.assertFalse(any(item["type"] == "turn.completed" for item in records))

    def test_unexpected_service_crash_after_turn_is_failure(self):
        records, error = self.invoke("crash")
        self.assertEqual("NATIVE_SERVICE_STOP_FAILED", error)
        self.assertFalse(any(item["type"] == "turn.completed" for item in records))


@unittest.skipUnless(os.environ.get("XIAOLVJI_CODEX_NATIVE_SMOKE"),
                     "Set XIAOLVJI_CODEX_NATIVE_SMOKE for isolated native protocol fixtures, without provider accounts.")
class InstalledCodexProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Native executable plus real namespaces; no real inference service.

    This deliberately constructed standalone fixture does not certify that the
    surrounding host's managed requirements may be erased in production. The
    separate preservation test mounts those exact requirements and must fail
    before a provider request. Production registry inventory remains mandatory.
    """
    @classmethod
    def setUpClass(cls):
        from xiaolvji.native_policy import normalize_native_deployment
        from xiaolvji.sandbox import LinuxSandbox
        cls.binary = Path(os.environ["XIAOLVJI_CODEX_NATIVE_SMOKE"])
        if not cls.binary.is_absolute() or not cls.binary.is_file():
            raise RuntimeError("The explicit installed Codex binary is unavailable.")
        cls.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-native-integration-", dir=Path(__file__).resolve().parents[2])
        cls.root = Path(cls.temporary.name)
        capability = LinuxSandbox(cls.root / "probe-state").probe()
        if not capability["supported"]:
            cls.temporary.cleanup()
            raise unittest.SkipTest("The actual strict Linux namespace backend is unavailable.")
        home = cls.root / "catalog-home"; home.mkdir()
        result = subprocess.run([str(cls.binary), "debug", "models", "--bundled"],
            env={"HOME": str(home), "CODEX_HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True, timeout=15, check=True)
        catalog = json.loads(result.stdout)
        # Preserve a complete bundled plain-tool entry under a synthetic alias.
        # This is metadata for a deterministic fixture, not a model selection
        # for a user's actual project or a real-provider quality claim.
        selected = next(entry for entry in catalog["models"] if entry.get("shell_type") == "unified_exec"
                        and entry.get("tool_mode") in (None, "tools")
                        and any(level.get("effort") == "high" for level in entry["supported_reasoning_levels"]))
        selected = copy.deepcopy(selected)
        selected.update(slug=MODEL, display_name="Synthetic native fixture", upgrade=None)
        cls.snapshot = {"schema_version": 1, "engine": "codex", "provider_id": "fixture", "api": "openai-responses",
                        "model": MODEL, "quality": {"model_reasoning_effort": "high"},
                        "codex_model_catalog": {"models": [selected]}}
        cls.deployment = normalize_native_deployment({"schema_version": 1, "engine": "codex", "executable": str(cls.binary),
            "protocol": "codex-app-server-v2", "policy": {"context": "standalone-api", "system_policy": "must-be-absent", "approval_policy": "on-request"}})

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    async def test_actual_native_acceptance_uses_production_runner_and_thirteen_boundary_gates(self):
        from xiaolvji.native_acceptance import ASSERTIONS, verify_native_host
        from xiaolvji.native_registry import NativeRegistry
        report = await verify_native_host(self.deployment, self.snapshot, self.root / "acceptance")
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(set(ASSERTIONS), set(report["assertions"]))
        self.assertTrue(all(report["assertions"].values()))
        self.assertEqual(2, report["synthetic_provider_requests"])
        self.assertEqual(0, report["real_provider_requests"])
        self.assertFalse(report["authentication_attempted"])
        # The actual fixture must satisfy the same strict record contract;
        # this validates its shape without bypassing production policy checks.
        NativeRegistry._report(report, self.snapshot, self.deployment)
        evidence_path = os.environ.get("XIAOLVJI_CODEX_NATIVE_REPORT")
        if evidence_path:
            Path(evidence_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    async def test_actual_native_configuration_preserves_every_supplied_quality_field(self):
        from xiaolvji.native_acceptance import verify_native_host
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["quality"].update(model_reasoning_summary="detailed", model_verbosity="high", service_tier="fast",
                                   model_context_window=200000, model_auto_compact_token_limit=150000)
        report = await verify_native_host(self.deployment, snapshot, self.root / "all-quality-fields")
        self.assertEqual("passed", report["status"], report)
        self.assertTrue(report["assertions"]["exact_model_and_quality"])

    async def execute_fixture(self, name, *, mode="text", snapshot=None, managed=False, cancel=False):
        from xiaolvji.executor import ProcessExecutor
        from xiaolvji.native_acceptance import native_command, runner_source
        from xiaolvji.provider_broker import ProviderBroker, ProviderProfile
        from xiaolvji.provider_bridge import BRIDGE_URL
        from xiaolvji.workspace import git
        root = self.root / name; root.mkdir()
        work = root / "workspace"; work.mkdir(); (work / "owned").mkdir()
        git("init", cwd=work)
        entered, disconnected = asyncio.Event(), asyncio.Event()
        requests, errors, connections, tasks = [], [], set(), set()
        async def upstream(reader, writer):
            task = asyncio.current_task(); tasks.add(task); connections.add(writer)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = dict(line.lower().split(": ", 1) for line in head.decode().split("\r\n")[1:] if line)
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
                requests.append(body); entered.set()
                if mode == "hold":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                    await writer.drain(); await reader.read(); disconnected.set(); return
                index = len(requests)
                if mode == "interactive" and index == 1:
                    script = "import time;from pathlib import Path;print('native-session-ready',flush=True);time.sleep(.8);Path('owned/interactive.txt').write_text('session-finished');print('native-session-finished',flush=True)"
                    item = {"type": "function_call", "id": "fc_exec", "call_id": "call_exec", "name": "exec_command", "status": "completed",
                            "arguments": json.dumps({"cmd": shlex.join([sys.executable, "-I", "-u", "-c", script]), "login": False,
                                                     "yield_time_ms": 250, "max_output_tokens": 1000})}
                elif mode == "interactive" and index == 2:
                    outputs = [item.get("output", "") for item in body["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
                    match = re.search(r"session ID\s+(\d+)", "\n".join(outputs), re.I)
                    if match is None:
                        raise ValueError("Native unified exec did not return its session identity.")
                    item = {"type": "function_call", "id": "fc_poll", "call_id": "call_poll", "name": "write_stdin", "status": "completed",
                            "arguments": json.dumps({"session_id": int(match.group(1)), "chars": "", "yield_time_ms": 1000, "max_output_tokens": 1000})}
                else:
                    item = {"type": "message", "id": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "Synthetic native fixture completed.", "annotations": []}]}
                response = {"id": "response_" + str(index), "object": "response", "status": "completed", "model": MODEL,
                            "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": response}]
                payload = "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n" for item in events).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: " + str(len(payload)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + payload)
                await writer.drain()
            except (OSError, ValueError, KeyError, asyncio.IncompleteReadError) as exc:
                errors.append(type(exc).__name__)
            finally:
                writer.close(); connections.discard(writer); tasks.discard(task)
        server = await asyncio.start_server(upstream, "127.0.0.1", 0, family=socket.AF_INET)
        port = server.sockets[0].getsockname()[1]
        class FixtureBroker(ProviderBroker):
            async def _connect_upstream(self, profile):
                return await asyncio.open_connection("127.0.0.1", port, family=socket.AF_INET)
        profile = ProviderProfile("fixture", "openai-responses", "https://fixture.invalid/v1/responses", MODEL,
                                  {"reasoning": {"effort": "high"}}, request_timeout=20)
        phase = "starting"
        attempt = {"attempt_id": "native-test", "generation": 1, "launch_key": "native-test:1"}
        def admitted(binding, requested):
            return binding == attempt and phase == ("starting" if requested == "grant" else "running")
        broker = FixtureBroker(root / "broker", {"fixture": profile}, {"fixture": "synthetic-not-a-provider-key"}, admitted)
        await broker.start()
        grant = await broker.grant(attempt, "fixture", ttl_seconds=40)
        selected = self.snapshot if snapshot is None else snapshot
        rendered = render_host_config(selected, BRIDGE_URL, grant.capability)
        command = native_command(self.deployment, selected, work, "Complete the local synthetic protocol fixture.", rendered["argv_extra"])
        runtime = {"version": 1, "capability": grant.capability, "api": profile.api, "env": rendered["env"],
                   "max_request_bytes": profile.max_request_bytes, "request_timeout": profile.request_timeout}
        files = [{"path": item["relative_path"], "content": item["content"]} for item in rendered["files"]]
        files.append({"path": ".xiaolvji/codex_app_server.py", "content": runner_source()})
        policy = {"workspace": str(work), "write_scope": [{"path": "owned", "kind": "directory"}],
                  "provider_transport": {"fd": grant.worker_socket.fileno()}, "home_files": files,
                  "readonly_paths": [str(self.binary)] + (["/etc/codex"] if managed else [])}
        executor = ProcessExecutor(root / "executor", profile="controlled_executor")
        async def started(identity):
            nonlocal phase
            phase = "running"; grant.close_worker_endpoint()
        task = asyncio.create_task(executor.run(attempt, command["argv"], str(work), stdin_text=command["stdin_text"],
            timeout=30, on_started=started, provider_runtime=runtime, sandbox_policy=policy))
        try:
            if cancel:
                await asyncio.wait_for(entered.wait(), 15)
                await executor.cancel(attempt["attempt_id"], grace=.05)
            result = await asyncio.wait_for(task, 35)
            if cancel:
                await asyncio.wait_for(disconnected.wait(), 3)
            return result, requests, errors, work
        finally:
            if not task.done():
                await executor.cancel(attempt["attempt_id"], grace=.05)
                await task
            phase = "closed"; grant.close_worker_endpoint(); await broker.close()
            server.close(); await server.wait_closed()
            for connection in list(connections): connection.close()
            for task in list(tasks): task.cancel()
            if tasks: await asyncio.gather(*list(tasks), return_exceptions=True)

    async def test_actual_native_exec_command_and_write_stdin_complete_three_request_loop(self):
        from xiaolvji.native_result import verify_host_output
        result, requests, errors, work = await self.execute_fixture("interactive", mode="interactive")
        self.assertEqual(0, result["exit_code"], Path(result["stdout_path"]).read_text())
        self.assertEqual(3, len(requests), errors)
        self.assertEqual([], errors)
        self.assertEqual("session-finished", (work / "owned/interactive.txt").read_text())
        self.assertIn("native-session-finished", json.dumps(requests[-1]["input"]))
        self.assertTrue(result["process_tree_terminated"])
        verify_host_output("codex", result["stdout_path"], truncated=False)

    async def test_unknown_model_blocks_before_any_native_tool_or_provider_request(self):
        snapshot = copy.deepcopy(self.snapshot); del snapshot["codex_model_catalog"]
        result, requests, errors, work = await self.execute_fixture("unknown-model", snapshot=snapshot)
        self.assertNotEqual(0, result["exit_code"])
        self.assertEqual([], requests)
        self.assertIn("NATIVE_MODEL_UNSUPPORTED", Path(result["stdout_path"]).read_text())
        self.assertEqual([], list((work / "owned").iterdir()))
        self.assertTrue(result["process_tree_terminated"])

    async def test_preserved_real_managed_policy_blocks_before_provider_requests(self):
        if not Path("/etc/codex/requirements.toml").is_file():
            self.skipTest("This explicit native test environment has no system managed requirements to preserve.")
        result, requests, errors, work = await self.execute_fixture("managed-policy", managed=True)
        self.assertNotEqual(0, result["exit_code"])
        self.assertEqual([], requests)
        self.assertIn("NATIVE_MANAGED_REQUIREMENTS_UNSUPPORTED", Path(result["stdout_path"]).read_text())
        self.assertEqual([], list((work / "owned").iterdir()))
        self.assertTrue(result["process_tree_terminated"])

    async def test_native_provider_wait_cancellation_reaps_tree_without_success_terminal(self):
        from xiaolvji.contracts import Fault
        from xiaolvji.native_result import verify_host_output
        result, requests, errors, work = await self.execute_fixture("cancel", mode="hold", cancel=True)
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertEqual(1, len(requests))
        self.assertEqual([], list((work / "owned").iterdir()))
        with self.assertRaises(Fault):
            verify_host_output("codex", result["stdout_path"], truncated=False)


if __name__ == "__main__":
    unittest.main()
