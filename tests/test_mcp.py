"""Wire-level MCP framing and authority tests; no real host compatibility claim."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Principal, digest
from xiaolvji.mcp import MAX_FRAME_BYTES, PROTOCOL_VERSION, serve
from xiaolvji.store import Ledger

ROOT = Path(__file__).resolve().parents[1]
READER = Principal("reader", "reader")
PLANNER = Principal("planner", "planner")
OPERATOR = Principal("operator", "operator")


def request(identifier, method, params=None):
    value = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        value["params"] = params
    return value


def initialize(version=PROTOCOL_VERSION):
    return request(1, "initialize", {"protocolVersion": version, "capabilities": {},
                                     "clientInfo": {"name": "test-client", "version": "1"}})


def initialized():
    return {"jsonrpc": "2.0", "method": "notifications/initialized"}


def content(response):
    return json.loads(response["result"]["content"][0]["text"])


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.db = self.home / "state" / "ledger.sqlite3"
        self.ledger = Ledger(self.db, global_limit=2)
        self.ledger.enable_project("demo", str(self.repo), {"project_limit": 2,
                                  "profile": "cooperative_native", "review_authority": "reviewer"}, OPERATOR, "enable")
        scope = [{"path": "src", "kind": "directory"}, {"path": "tests", "kind": "directory"}]
        self.plan = {"id": "plan", "revision": 1, "scope_digest": digest(scope),
                     "approved_scope": scope, "approval_ref": "test:approved-plan", "contracts": []}
        self.ledger.register_plan("demo", self.plan, PLANNER, "plan")

    def task(self):
        task = json.loads((ROOT / "examples" / "task.json").read_text())
        task["task_id"] = "new-task"
        task["plan"] = {key: self.plan[key] for key in ("id", "revision", "scope_digest")}
        task["depends_on"] = []
        task["contract_versions"] = []
        return task

    def run_frames(self, frames, principal=READER, ledger=None):
        stream = io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
        output = io.StringIO()
        result = serve(ledger or self.ledger, principal, stream, output)
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_legacy_handshake_negotiation_and_tools_only_capability(self):
        status, frames = self.run_frames([initialize("2030-01-01"), initialized(), request(2, "tools/list")])
        self.assertEqual(status, 0)
        self.assertEqual(frames[0]["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(frames[0]["result"]["capabilities"], {"tools": {"listChanged": False}})
        names = {tool["name"] for tool in frames[1]["result"]["tools"]}
        self.assertEqual(names, {"parallel_status", "parallel_queue", "parallel_metrics", "parallel_events",
                                 "parallel_project_tree", "parallel_read_file"})
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in frames[1]["result"]["tools"]))

    def test_planner_tools_include_schema_registration_but_no_execution(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/list")], PLANNER)
        names = {tool["name"] for tool in frames[-1]["result"]["tools"]}
        self.assertEqual(names, {"parallel_status", "parallel_queue", "parallel_metrics", "parallel_events",
                                 "parallel_project_tree", "parallel_read_file", "parallel_register_task"})
        registration = frames[-1]["result"]["tools"][-1]
        self.assertFalse(registration["annotations"]["readOnlyHint"])
        self.assertEqual(registration["inputSchema"]["properties"]["task"]["properties"]["schema_version"], {"const": 1})

    def test_tools_require_initialized_notification_ping_does_not(self):
        _, frames = self.run_frames([request(0, "ping"), initialize(), request(2, "tools/list"),
                                     initialized(), request(3, "tools/list")])
        self.assertEqual(frames[0]["result"], {})
        self.assertEqual(frames[2]["error"]["code"], -32002)
        self.assertIn("tools", frames[3]["result"])

    def test_modern_discovery_returns_nonmodern_method_error_for_fallback(self):
        _, frames = self.run_frames([request(0, "server/discover", {"_meta": {"protocolVersion": "2026-07-28"}}), initialize(), initialized()])
        self.assertEqual(frames[0]["error"]["code"], -32601)
        self.assertEqual(frames[1]["result"]["protocolVersion"], PROTOCOL_VERSION)

    def test_authorized_task_registration_uses_ledger_and_idempotency(self):
        args = {"project_id": "demo", "task": self.task(), "request_id": "register-once"}
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {"name": "parallel_register_task", "arguments": args}),
                                     request(3, "tools/call", {"name": "parallel_register_task", "arguments": args})], PLANNER)
        self.assertFalse(frames[1]["result"]["isError"])
        self.assertEqual(content(frames[1]), content(frames[2]))
        self.assertEqual(len(self.ledger.status()["tasks"]), 1)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)

    def test_reader_cannot_register_by_calling_unadvertised_tool(self):
        args = {"project_id": "demo", "task": self.task(), "request_id": "rogue-registration"}
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {"name": "parallel_register_task", "arguments": args})])
        self.assertTrue(frames[-1]["result"]["isError"])
        self.assertEqual(content(frames[-1])["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(self.ledger.status()["tasks"], [])

    def test_role_in_arguments_cannot_replace_transport_authority(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_status", "arguments": {"project_id": "demo", "role": "operator"}})])
        self.assertTrue(frames[-1]["result"]["isError"])
        self.assertEqual(content(frames[-1])["error"]["code"], "INVALID_INPUT")

    def test_notifications_have_no_response_and_never_register(self):
        notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "parallel_register_task",
                        "arguments": {"project_id": "demo", "task": self.task(), "request_id": "notification-write"}}}
        _, frames = self.run_frames([initialize(), initialized(), notification,
                                     {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
                                     request(2, "ping")], PLANNER)
        self.assertEqual(len(frames), 2)
        self.assertEqual(self.ledger.status()["tasks"], [])

    def test_unknown_tool_and_malformed_call_are_protocol_errors(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {"name": "parallel_review"}),
                                     request(3, "tools/call", {"name": "parallel_status", "arguments": []})])
        self.assertEqual(frames[1]["error"]["code"], -32602)
        self.assertEqual(frames[2]["error"]["code"], -32602)

    def test_bad_tool_values_return_iserror_not_traceback(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_events", "arguments": {"after": 0, "limit": True}}),
            request(3, "tools/call", {"name": "parallel_status", "arguments": {"project_id": "missing"}})])
        self.assertEqual(content(frames[1])["error"]["code"], "INVALID_INPUT")
        self.assertEqual(content(frames[2])["error"]["code"], "NOT_FOUND")
        self.assertNotIn("Traceback", json.dumps(frames))

    def test_bounded_event_cursor(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_events", "arguments": {"after": 0, "limit": 1}})])
        data = content(frames[-1])
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["next_cursor"], data["events"][0]["seq"])

    def test_queue_and_metrics_schemas_are_closed_optional_and_read_only(self):
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/list")])
        tools = {tool["name"]: tool for tool in frames[-1]["result"]["tools"]}
        for name, fields in (("parallel_queue", {"project_id", "limit"}),
                             ("parallel_metrics", {"project_id"})):
            with self.subTest(tool=name):
                tool = tools[name]
                self.assertEqual(fields, set(tool["inputSchema"]["properties"]))
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                self.assertFalse(tool["inputSchema"].get("required", []))
                self.assertEqual({"readOnlyHint": True, "destructiveHint": False,
                                  "idempotentHint": True, "openWorldHint": False}, tool["annotations"])
        self.assertEqual({"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                         tools["parallel_queue"]["inputSchema"]["properties"]["limit"])

    def test_reader_queue_and_metrics_are_bounded_observations_without_mutation(self):
        for index in range(2):
            task = self.task()
            task["task_id"] = f"observed-{index}"
            self.ledger.register_task("demo", task, PLANNER, f"observed-register-{index}")
        before = self.ledger.status()
        history = self.ledger.events()
        calls = [("parallel_queue", {"project_id": "demo", "limit": 1}),
                 ("parallel_metrics", {"project_id": "demo"}),
                 ("parallel_queue", {}), ("parallel_metrics", {}),
                 ("parallel_queue", {"limit": 200})]
        _, frames = self.run_frames([initialize(), initialized(), *[
            request(index + 2, "tools/call", {"name": name, "arguments": arguments})
            for index, (name, arguments) in enumerate(calls)]])
        for frame in frames[1:]:
            self.assertFalse(frame["result"]["isError"], content(frame))
        queue = content(frames[1])
        self.assertTrue(queue["advisory"])
        self.assertEqual(1, len(queue["entries"]))
        self.assertEqual(2, queue["total"])
        self.assertTrue(queue["truncated"])
        self.assertEqual(before["capacity"], queue["capacity"])
        self.assertEqual(2, len(content(frames[3])["entries"]))
        for index, project_id in ((2, "demo"), (4, None)):
            metrics = content(frames[index])
            self.assertTrue(metrics["diagnostic_only"])
            self.assertEqual(project_id, metrics["project_id"])
            self.assertEqual(2, metrics["population"]["tasks"])
            self.assertEqual(before["capacity"], metrics["capacity"]["global"])
        self.assertEqual(before, self.ledger.status())
        self.assertEqual(history, self.ledger.events())

    def test_queue_and_metrics_reject_unknown_typed_and_mutation_arguments(self):
        calls = []
        for name in ("parallel_queue", "parallel_metrics"):
            calls.extend((name, {"project_id": invalid}) for invalid in (None, True, 1, "", [], {}))
            calls.extend((name, {field: value}) for field, value in (
                ("role", "operator"), ("principal", {"id": "operator", "role": "operator"}),
                ("request_id", "unauthorized-write"), ("task", self.task()),
                ("decision", "approve"), ("start", True), ("reserve", True)))
        calls.extend(("parallel_queue", {"limit": invalid})
                     for invalid in (None, True, False, 0, -1, 201, 1.5, "1"))
        calls.extend((("parallel_metrics", {"limit": 1}), ("parallel_queue", {"after": 0})))
        before, history = self.ledger.status(), self.ledger.events()
        _, frames = self.run_frames([initialize(), initialized(), *[
            request(index + 2, "tools/call", {"name": name, "arguments": arguments})
            for index, (name, arguments) in enumerate(calls)]])
        for frame in frames[1:]:
            self.assertTrue(frame["result"]["isError"])
            self.assertEqual("INVALID_INPUT", content(frame)["error"]["code"])
        self.assertEqual(before, self.ledger.status())
        self.assertEqual(history, self.ledger.events())

    def test_queue_and_metrics_require_argument_objects_and_known_projects(self):
        for name in ("parallel_queue", "parallel_metrics"):
            for arguments in (None, True, []):
                with self.subTest(tool=name, arguments=arguments):
                    _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
                        "name": name, "arguments": arguments})])
                    self.assertEqual(-32602, frames[-1]["error"]["code"])
            _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
                "name": name, "arguments": {"project_id": "missing"}})])
            self.assertTrue(frames[-1]["result"]["isError"])
            self.assertEqual("NOT_FOUND", content(frames[-1])["error"]["code"])

    def create_repository(self):
        env = {"PATH": os.defpath, "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Synthetic test",
               "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Synthetic test",
               "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.repo, env=env, capture_output=True,
                                  text=True, timeout=10, check=True).stdout.strip()
        git("-c", "init.defaultBranch=main", "init", "-q")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "module.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
        git("add", "--all")
        git("commit", "-q", "--no-gpg-sign", "-m", "source fixture")
        return git("rev-parse", "HEAD")

    def test_reader_can_inspect_committed_snapshot_without_worktree_or_mutation_access(self):
        commit = self.create_repository()
        before = self.ledger.status()["event_cursor"]
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_project_tree", "arguments": {"project_id": "demo", "directory": "src", "limit": 1}})])
        tree = content(frames[-1])
        self.assertFalse(frames[-1]["result"]["isError"])
        self.assertEqual(commit, tree["commit"])
        self.assertEqual("src/module.py", tree["entries"][0]["path"])
        (self.repo / "src" / "module.py").write_text("synthetic-uncommitted-secret")
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_read_file", "arguments": {"project_id": "demo", "commit": tree["commit"], "path": "src/module.py"}})])
        result = content(frames[-1])
        self.assertEqual("VALUE = 'committed'\n", result["content"])
        self.assertNotIn("synthetic-uncommitted-secret", json.dumps(frames))
        self.assertEqual(before, self.ledger.status()["event_cursor"])
        self.assertEqual(0, self.ledger.status()["capacity"]["used"])

    def test_repository_tools_reject_extra_authority_and_invalid_input_shapes(self):
        calls = [
            ("parallel_project_tree", {"project_id": "demo", "role": "operator"}),
            ("parallel_project_tree", {"project_id": "demo", "commit": None}),
            ("parallel_project_tree", {"project_id": "demo", "cursor": True}),
            ("parallel_read_file", {"project_id": "demo", "commit": "HEAD", "path": "src/module.py"}),
            ("parallel_read_file", {"project_id": "demo", "commit": "a" * 40, "path": "../state/ledger.sqlite3"}),
            ("parallel_read_file", {"project_id": "demo", "commit": "a" * 40, "path": "src/module.py", "max_bytes": True}),
        ]
        _, frames = self.run_frames([initialize(), initialized(), *[
            request(index + 2, "tools/call", {"name": name, "arguments": arguments})
            for index, (name, arguments) in enumerate(calls)]])
        for frame in frames[1:]:
            self.assertTrue(frame["result"]["isError"])
            self.assertEqual("INVALID_INPUT", content(frame)["error"]["code"])

    def test_repository_tools_reject_disabled_project(self):
        self.ledger.disable_project("demo", OPERATOR, "disable")
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {
            "name": "parallel_project_tree", "arguments": {"project_id": "demo"}})])
        self.assertTrue(frames[-1]["result"]["isError"])
        self.assertEqual("PROJECT_DISABLED", content(frames[-1])["error"]["code"])

    def test_duplicate_and_invalid_json_are_errors_then_session_can_start(self):
        stream = io.StringIO('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n[1,2]\n' + json.dumps(initialize()) + "\n")
        output = io.StringIO()
        self.assertEqual(serve(self.ledger, READER, stream, output), 0)
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(frames[0]["error"]["code"], -32700)
        self.assertEqual(frames[1]["error"]["code"], -32600)
        self.assertIn("result", frames[2])

    def test_null_and_boolean_request_ids_rejected(self):
        _, frames = self.run_frames([request(None, "ping"), request(True, "ping")])
        for frame in frames:
            self.assertEqual(frame["error"]["code"], -32600)
            self.assertIsNone(frame["id"])

    def test_oversized_input_fails_bounded_and_stops_connection(self):
        stream = io.BytesIO(b"x" * (MAX_FRAME_BYTES + 100) + b"\n")
        output = io.BytesIO()
        self.assertEqual(serve(self.ledger, READER, stream, output), 1)
        self.assertLess(len(output.getvalue()), 300)
        self.assertEqual(json.loads(output.getvalue())["error"]["data"]["code"], "INPUT_TOO_LARGE")
        self.assertEqual(stream.tell(), MAX_FRAME_BYTES + 1)

    def test_tool_rate_is_bounded_without_sleeping(self):
        frames = [initialize(), initialized()] + [request(i + 2, "tools/call", {"name": "parallel_status", "arguments": {}}) for i in range(61)]
        with patch("xiaolvji.mcp.time.monotonic", return_value=42.0):
            _, output = self.run_frames(frames)
        self.assertEqual(content(output[-1])["error"]["code"], "RATE_LIMITED")

    def test_unexpected_exception_is_sanitized(self):
        class BrokenLedger:
            def status(self, project_id):
                raise RuntimeError("secret-token-do-not-expose")
        _, frames = self.run_frames([initialize(), initialized(), request(2, "tools/call", {"name": "parallel_status"})], ledger=BrokenLedger())
        self.assertEqual(content(frames[-1])["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("secret-token", json.dumps(frames))

    def test_real_subprocess_stdio_frames(self):
        entry = ("from xiaolvji.store import Ledger; from xiaolvji.contracts import Principal; "
                 "from xiaolvji.mcp import serve; import sys; "
                 "raise SystemExit(serve(Ledger(sys.argv[1]),Principal('reader','reader')))")
        frames = [initialize(), initialized(), request(2, "tools/call", {"name": "parallel_status", "arguments": {"project_id": "demo"}})]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([sys.executable, "-c", entry, str(self.db)],
                                input="".join(json.dumps(frame) + "\n" for frame in frames),
                                capture_output=True, text=True, env=environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(content(responses[-1])["capacity"]["limit"], 2)


if __name__ == "__main__":
    unittest.main()
