from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from xiaolvji.contracts import Fault, bytes_digest
from xiaolvji.native_result import MAX_STDOUT_BYTES, verify_host_output


class NativeResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-native-result-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "stdout.log"

    def codex(self):
        # Shape observed from the installed CLI and its documented JSONL path.
        return [
            {"type": "thread.started", "thread_id": "synthetic-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
        ]

    def zcode(self):
        identity = {"sessionId": "synthetic-session", "turnId": "synthetic-turn"}
        return [
            {"type": "turn.started", **identity, "payload": {}},
            {"type": "message.upserted", **identity, "payload": {"content": "done"}},
            {"type": "turn.completed", **identity, "payload": {"resultType": "success", "response": "done"}},
            {"type": "result", **identity, "response": "done", "eventCount": 3,
             "projection": {"status": "idle", "turnCount": 1}},
        ]

    def write(self, events):
        data = b"".join(json.dumps(event, ensure_ascii=True).encode() + b"\n" for event in events)
        self.output.write_bytes(data)
        return data

    def rejected(self, engine, events, code="NATIVE_RESULT_INVALID", **kwargs):
        self.write(events)
        with self.assertRaises(Fault) as raised:
            verify_host_output(engine, self.output, **kwargs)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_codex_success_binds_complete_log_without_exit_authority(self):
        data = self.write(self.codex())
        result = verify_host_output("codex", self.output, expected_session_id="synthetic-thread")
        self.assertEqual("completed", result["status"])
        self.assertEqual("synthetic-thread", result["session_id"])
        self.assertEqual(4, result["event_count"])
        self.assertIsNone(result["turn_id"])
        self.assertEqual(bytes_digest(data), result["stdout_digest"])
        self.assertEqual(len(data), result["stdout_bytes"])
        self.assertFalse(result["process_tree_terminated"])
        self.assertNotIn("done", json.dumps(result))

    def test_zcode_success_uses_final_projection_and_matched_turn(self):
        for status in ("idle", "completed"):
            with self.subTest(status=status):
                events = self.zcode()
                events[-1]["projection"]["status"] = status
                data = self.write(events)
                result = verify_host_output("zcode", self.output)
                self.assertEqual("synthetic-session", result["session_id"])
                self.assertEqual("synthetic-turn", result["turn_id"])
                self.assertEqual("result", result["terminal_type"])
                self.assertEqual(bytes_digest(data), result["stdout_digest"])
                self.assertFalse(result["process_tree_terminated"])

    def test_recovered_tool_command_failure_is_not_host_turn_failure(self):
        events = self.codex()
        events.insert(2, {"type": "item.completed", "item": {
            "type": "command_execution", "id": "tool_1", "status": "failed", "exit_code": 1}})
        self.write(events)
        self.assertEqual("completed", verify_host_output("codex", self.output)["status"])
        events = self.zcode()
        events.insert(1, {"type": "tool.updated", "sessionId": "synthetic-session",
                          "turnId": "synthetic-turn", "payload": {"kind": "error"}})
        self.write(events)
        self.assertEqual("completed", verify_host_output("zcode", self.output)["status"])

    def test_unknown_bounded_nonterminal_events_are_observable(self):
        events = self.codex()
        events.insert(2, {"type": "future.progress", "data": {"phase": "working"}})
        self.write(events)
        result = verify_host_output("codex", self.output)
        self.assertEqual(5, result["event_count"])

    def test_empty_and_incomplete_logs_fail(self):
        for engine, events in (("codex", []), ("codex", self.codex()[:-1]),
                               ("zcode", []), ("zcode", self.zcode()[:-1])):
            with self.subTest(engine=engine, size=len(events)):
                self.rejected(engine, events)

    def test_codex_requires_actual_thread_and_single_turn_start(self):
        original = self.codex()
        cases = [original[1:], original[:1] + original[2:],
                 original[:2] + [original[1]] + original[2:],
                 [original[0], original[0]] + original[1:],
                 [{"type": "future.event", "thread_id": "synthetic-thread"}] + original[1:],
                 [original[1], original[0]] + original[2:]]
        for events in cases:
            with self.subTest(events=events):
                self.rejected("codex", events)

    def test_codex_errors_cannot_become_exit_zero_success(self):
        for error in ({"type": "error", "message": "private provider diagnostic"},
                      {"type": "turn.failed", "error": {"message": "private provider diagnostic"}},
                      {"type": "item.completed", "item": {"type": "error", "message": "private provider diagnostic"}}):
            with self.subTest(kind=error["type"]):
                events = self.codex()
                events.insert(2, error)
                fault = self.rejected("codex", events, "NATIVE_TURN_FAILED")
                self.assertNotIn("private provider diagnostic", json.dumps(fault.as_dict()))

    def test_codex_completed_with_error_fails(self):
        events = self.codex()
        events[-1]["error"] = {"message": "private"}
        self.rejected("codex", events, "NATIVE_TURN_FAILED")

    def test_duplicate_terminal_and_events_after_terminal_fail(self):
        for engine, original in (("codex", self.codex()), ("zcode", self.zcode())):
            for extra in (original[-1], {"type": "future.progress"}):
                with self.subTest(engine=engine, extra=extra["type"]):
                    self.rejected(engine, original + [extra])

    def test_session_identity_must_be_consistent_and_expected(self):
        for engine, events in (("codex", self.codex()), ("zcode", self.zcode())):
            with self.subTest(engine=engine, mismatch="expected"):
                self.rejected(engine, events, expected_session_id="different")
            key = "thread_id" if engine == "codex" else "sessionId"
            events[2][key] = "different"
            with self.subTest(engine=engine, mismatch="observed"):
                self.rejected(engine, events)

    def test_missing_or_invalid_session_identity_fails(self):
        for engine, original in (("codex", self.codex()), ("zcode", self.zcode())):
            key = "thread_id" if engine == "codex" else "sessionId"
            for bad in (None, "", 1, "embedded\nnewline", "\ud800"):
                events = copy.deepcopy(original)
                events[0][key] = bad
                with self.subTest(engine=engine, bad=repr(bad)):
                    self.rejected(engine, events)

    def test_zcode_error_projection_cannot_pass_after_zero_exit(self):
        for status in ("error", "running", "waiting", "paused", "unknown", None):
            events = self.zcode()
            events[-1]["projection"]["status"] = status
            with self.subTest(status=status):
                self.rejected("zcode", events, "NATIVE_TURN_FAILED")

    def test_zcode_cancel_or_budget_exhaustion_is_not_success(self):
        for result_type in ("cancelled", "error_max_turns", "error_max_budget",
                            "error_during_execution", "error_max_tool_calls", "future_outcome"):
            events = self.zcode()
            events[-2]["payload"]["resultType"] = result_type
            with self.subTest(result_type=result_type):
                self.rejected("zcode", events, "NATIVE_TURN_FAILED")

    def test_zcode_turn_failure_is_not_repaired_by_result_line(self):
        events = self.zcode()
        events.insert(1, {"type": "turn.failed", "sessionId": "synthetic-session",
                          "turnId": "synthetic-turn", "payload": {"error": "private"}})
        self.rejected("zcode", events, "NATIVE_TURN_FAILED")

    def test_zcode_requires_matching_turn_identifiers(self):
        for index in (0, 1, 2, 3):
            for value in ("different", None):
                if index == 1 and value is None:
                    continue  # Optional unrelated nonterminal IDs may be absent.
                events = self.zcode()
                events[index]["turnId"] = value
                with self.subTest(index=index, value=value):
                    self.rejected("zcode", events)

    def test_zcode_requires_one_start_and_successful_turn_end(self):
        original = self.zcode()
        cases = [original[1:], original[:2] + original[3:],
                 [original[0], original[0]] + original[1:],
                 original[:3] + [original[2], original[3]],
                 [original[-1]]]
        for events in cases:
            with self.subTest(size=len(events)):
                self.rejected("zcode", events)
        events = self.zcode()
        del events[-2]["payload"]["resultType"]
        self.rejected("zcode", events)

    def test_zcode_result_shape_is_required(self):
        for key, value in (("projection", None), ("response", None),
                           ("eventCount", True), ("eventCount", -1), ("eventCount", 65_537)):
            events = self.zcode()
            events[-1][key] = value
            with self.subTest(key=key, value=value):
                self.rejected("zcode", events)

    def test_blank_lines_do_not_change_lifecycle(self):
        data = self.write(self.codex())
        self.output.write_bytes(b"\n\r\n" + data.replace(b"\n", b"\n\n") + b"\t\n")
        self.assertEqual(4, verify_host_output("codex", self.output)["event_count"])

    def test_malformed_ambiguous_or_nonunicode_json_fails(self):
        valid = self.write(self.codex())
        cases = [b'{"type":"error","type":"turn.completed"}', b"[]", b"{}",
                 b'{"type":"x","value":NaN}', b'{"type":"x","value":"\\ud800"}',
                 b"\xff", b'{"type":"x","value":' + b"[" * 60 + b"0" + b"]" * 60 + b"}"]
        for bad in cases:
            self.output.write_bytes(valid.split(b"\n", 1)[0] + b"\n" + bad + b"\n" + b"\n".join(valid.split(b"\n")[1:]))
            with self.subTest(bad=bad[:60]), self.assertRaises(Fault) as raised:
                verify_host_output("codex", self.output)
            self.assertEqual("NATIVE_RESULT_INVALID", raised.exception.code)

    def test_truncated_log_is_explicitly_rejected_even_with_final_event(self):
        self.write(self.codex())
        with self.assertRaises(Fault) as raised:
            verify_host_output("codex", self.output, truncated=True)
        self.assertIn("truncated", raised.exception.message)
        with self.assertRaises(Fault):
            verify_host_output("codex", self.output, truncated=0)

    def test_oversized_log_and_event_fail_boundedly(self):
        self.output.write_bytes(b" " * (MAX_STDOUT_BYTES + 1))
        with self.assertRaises(Fault):
            verify_host_output("codex", self.output)
        self.output.write_bytes(b'{"type":"future.event","data":"' + b"a" * 1_048_576 + b'"}\n')
        with self.assertRaises(Fault):
            verify_host_output("codex", self.output)

    def test_excessive_small_event_count_is_rejected(self):
        self.output.write_bytes(b'{"type":"future.event"}\n' * 65_537)
        with self.assertRaises(Fault) as raised:
            verify_host_output("codex", self.output)
        self.assertIn("event-count", raised.exception.message)

    def test_symlink_paths_and_nonregular_files_are_rejected(self):
        self.write(self.codex())
        link = self.root / "link.log"
        link.symlink_to(self.output)
        parent = self.root / "linked"
        parent.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        for path in (link, parent / "stdout.log", self.root, fifo, self.root / "missing"):
            with self.subTest(path=path.name), self.assertRaises(Fault):
                verify_host_output("codex", path)

    def test_file_mutation_during_read_is_rejected(self):
        self.write(self.codex())
        before = self.output.stat()
        after = SimpleNamespace(**{key: getattr(before, key) for key in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")})
        after.st_mtime_ns += 1
        with mock.patch("xiaolvji.native_result.os.fstat", side_effect=[before, after]):
            with self.assertRaises(Fault) as raised:
                verify_host_output("codex", self.output)
        self.assertIn("changed", raised.exception.message)

    def test_unsupported_engine_and_relative_path_fail(self):
        self.write(self.codex())
        with self.assertRaises(Fault) as raised:
            verify_host_output("unsupported", self.output)
        self.assertEqual("CAPABILITY_UNSUPPORTED", raised.exception.code)
        with self.assertRaises(Fault):
            verify_host_output("codex", Path("stdout.log"))


if __name__ == "__main__":
    unittest.main()
