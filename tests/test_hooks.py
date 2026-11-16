from __future__ import annotations

import json
import unittest

from xiaolvji.contracts import Fault
from xiaolvji.hooks import COORDINATOR_MCP_PREFIXES, COORDINATOR_MCP_TOOLS, handle, normalize_input


class HookTests(unittest.TestCase):
    def context(self, **extra):
        return {"enrolled": True, "project_id": "p1", "profile": "controlled_executor",
                "controller_owns_queue": True, "identity_verified": True,
                "capacity": 4, "occupied": 2, "ready_count": 2, **extra}

    def pre(self, tool="Agent"):
        return {"hook_event_name": "PreToolUse", "session_id": "parent", "tool_name": tool,
                "tool_use_id": "use-1", "tool_input": {"prompt": "task"}}

    def test_unenrolled_hooks_do_not_inject_or_decide(self):
        self.assertEqual(handle("codex", {"hook_event_name": "SessionStart"}), {})
        self.assertEqual(handle("zcode", self.pre()), {})
        self.assertEqual(handle("codex", self.pre(), {"enrolled": False}), {})

    def test_start_context_is_english_and_bounded(self):
        result = handle("zcode", {"hookEventName": "SessionStart"}, self.context(blockers=["x" * 900] * 100))
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("approved scope", text)
        self.assertIn("Controller status:", text)
        self.assertLess(len(text), 3000)
        self.assertNotIn("permissionDecision", json.dumps(result))

    def test_controlled_native_spawn_and_resume_are_denied(self):
        for tool in ("Agent", "Task", "spawn_agent", "followup_task", "resume_agent", "SendMessage",
                     "Workflow", "CreateWorkflow", "ResumeWorkflowRun", "mcp__any__spawn_agent"):
            with self.subTest(tool=tool):
                result = handle("codex", self.pre(tool), self.context())
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_cooperative_controller_queue_still_has_one_owner(self):
        result = handle("zcode", self.pre("Agent"), self.context(profile="cooperative_native"))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        result = handle("zcode", self.pre("Agent"), self.context(profile="cooperative_native", controller_owns_queue=False))
        self.assertEqual(result, {})

    def test_worker_cannot_recursively_spawn(self):
        result = handle("codex", self.pre("spawn_agent"), self.context(profile="cooperative_native", controller_owns_queue=False, actor_role="worker"))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_parent_session_identity_does_not_authorize_worker(self):
        result = handle("codex", self.pre("exec_command"), self.context(identity_verified=False))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("session ID", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_strict_coordinator_can_defer_only_exact_controller_mcp_tools(self):
        for adapter, prefixes in COORDINATOR_MCP_PREFIXES.items():
            for prefix in prefixes:
                for tool in COORDINATOR_MCP_TOOLS:
                    with self.subTest(adapter=adapter, tool=prefix + tool):
                        result = handle(adapter, self.pre(prefix + tool), self.context(identity_verified=False))
                        self.assertEqual({}, result)
                        self.assertNotIn("allow", json.dumps(result))

    def test_coordinator_namespace_cannot_be_spoofed_by_suffix_case_or_payload(self):
        names = ("parallel_status", "mcp__other__parallel_status", "mcp__parallel-development__exec_command",
                 "mcp__parallel-development__parallel_status__suffix", "prefix.mcp__parallel-development__parallel_status",
                 "mcp__Parallel-development__parallel_status", "mcp__parallel-development__PARALLEL_STATUS",
                 "mcp__plugin_other_parallel-development__parallel_status", "mcp__parallel-development__parallel_review",
                 "parallel_queue", "parallel_metrics", "mcp__other__parallel_queue",
                 "mcp__parallel-development__parallel_queue__reserve",
                 "mcp__parallel-development__parallel_metrics__approve",
                 "mcp__parallel-development__PARALLEL_METRICS")
        for adapter in ("codex", "zcode"):
            for name in names:
                payload = {**self.pre(name), "actor_role": "coordinator", "identity_verified": True}
                with self.subTest(adapter=adapter, name=name):
                    result = handle(adapter, payload, self.context(identity_verified=False))
                    self.assertEqual("deny", result["hookSpecificOutput"]["permissionDecision"])

    def test_worker_identity_and_cancellation_are_not_bypassed_by_controller_names(self):
        for adapter in ("codex", "zcode"):
            for name in ("parallel_status", "parallel_queue", "parallel_metrics"):
                tool = COORDINATOR_MCP_PREFIXES[adapter][0] + name
                for verified, cancelled in ((False, False), (False, True), (True, True)):
                    with self.subTest(adapter=adapter, tool=tool, verified=verified, cancelled=cancelled):
                        result = handle(adapter, self.pre(tool), self.context(actor_role="worker", identity_verified=verified, cancelled=cancelled))
                        self.assertEqual("deny", result["hookSpecificOutput"]["permissionDecision"])

    def test_hook_coordinator_allowlist_matches_the_complete_mcp_surface(self):
        from xiaolvji.mcp import _TOOL_NAMES
        self.assertEqual(COORDINATOR_MCP_TOOLS, _TOOL_NAMES)
        self.assertEqual(7, len(COORDINATOR_MCP_TOOLS))
        self.assertTrue({"parallel_queue", "parallel_metrics"}.issubset(COORDINATOR_MCP_TOOLS))

    def test_no_unconditional_allow(self):
        for adapter in ("codex", "zcode"):
            self.assertEqual(handle(adapter, self.pre("read_file"), self.context()), {})
        self.assertEqual(handle("zcode", {"hookEventName": "PermissionRequest"}, self.context()), {})

    def test_stop_requires_prior_durable_continuation_reservation(self):
        stop = {"hook_event_name": "Stop"}
        self.assertEqual(handle("codex", stop, self.context()), {})
        result = handle("codex", stop, self.context(continuation_reserved=True, stop_continuations=0))
        self.assertEqual(result["decision"], "block")
        for extra in ({"stop_continuations": 2}, {"ready_count": 0}, {"occupied": 4},
                      {"cancelled": True}, {"paused": True}, {"profile": "blocked"}):
            ctx = self.context(continuation_reserved=True, stop_continuations=0)
            ctx.update(extra)
            self.assertEqual(handle("codex", stop, ctx), {})

    def test_native_terminal_events_never_release_or_restart(self):
        for event in ("SubagentStop", "Interrupt", "PostToolUse"):
            self.assertEqual(handle("codex", {"hook_event_name": event}, self.context()), {})
        self.assertEqual(handle("zcode", {"hookEventName": "PostToolUse", "toolResponse": {"status": "async_launched"}}, self.context()), {})

    def test_aliases_accept_only_equal_json_values(self):
        normalized = normalize_input("zcode", {"hook_event_name": "PreToolUse", "hookEventName": "PreToolUse",
                                               "tool_name": "Agent", "toolName": "Agent", "tool_input": {"a": 1}, "toolInput": {"a": 1}})
        self.assertEqual(normalized["tool_name"], "Agent")
        conflicts = [
            {"hook_event_name": "PreToolUse", "hookEventName": "Stop", "tool_name": "Agent"},
            {**self.pre(), "sessionId": "different"},
            {**self.pre(), "toolName": "exec_command"},
            {**self.pre(), "tool_input": {"a": True}, "toolInput": {"a": 1}},
        ]
        for payload in conflicts:
            with self.assertRaises(Fault):
                handle("zcode", payload, self.context())

    def test_malformed_inputs_fail_explicitly(self):
        for payload in ([], {}, {"hook_event_name": "PreToolUse"},
                        {"hook_event_name": "Stop", "stopHookActive": "false"},
                        {"hook_event_name": "Stop", "cwd": "bad\x00path"}):
            with self.subTest(payload=payload), self.assertRaises(Fault):
                handle("codex", payload, self.context())


if __name__ == "__main__":
    unittest.main()
