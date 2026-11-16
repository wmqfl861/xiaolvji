"""Semantic result/recovery gates with real processes and synthetic host JSONL.

These commands do not invoke a model or native CLI. They isolate the pipeline's
handling of exit-zero failures and durable transcript recovery.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_pipeline as fixtures
from xiaolvji.authority import atomic_json
from xiaolvji.contracts import read_json


class NativePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.PipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.pipeline = self.fixture.pipeline
        self.pipeline.native_executor = self.fixture.executor
        self.pipeline.auto_verify = False
        self.pipeline.auto_integrate = False
        prepare = self.pipeline.prepare
        async def marked_prepare(attempt):
            job = await prepare(attempt)
            job["native_engine"] = "codex"
            atomic_json(self.pipeline.root / "prepared" / (attempt["attempt_id"] + ".json"), job)
            return job
        self.pipeline.prepare = marked_prepare

    def add(self, *, success=True):
        events = [{"type": "thread.started", "thread_id": "fixture-native-thread"},
                  {"type": "turn.started"},
                  {"type": "turn.completed"} if success else {"type": "turn.failed", "error": {"message": "fixture failure"}}]
        wire = "\n".join(json.dumps(event) for event in events)
        self.fixture.add("native", "from pathlib import Path; Path('src/native.txt').write_text('candidate'); print(" + repr(wire) + ")",
                         "assert True")

    async def test_success_requires_complete_native_result_before_candidate(self):
        self.add()
        result = await self.fixture.dispatch()
        self.assertEqual(result["errors"], [])
        task = self.fixture.ledger.task("fixture", "native")
        self.assertEqual(task["state"], "SUBMITTED")
        self.assertTrue(task["candidate"]["native_result_digest"].startswith("sha256:"))
        reports = list((self.pipeline.root / "native-results").glob("*.json"))
        self.assertEqual(len(reports), 1)
        self.assertFalse(read_json(reports[0])["report"]["process_tree_terminated"])
        self.assertEqual(result["status"]["capacity"]["used"], 0)

    async def test_exit_zero_with_failed_turn_cannot_snapshot_candidate(self):
        self.add(success=False)
        result = await self.fixture.dispatch()
        task = self.fixture.ledger.task("fixture", "native")
        self.assertEqual(task["state"], "BLOCKED")
        self.assertIsNone(task["candidate"])
        self.assertEqual(result["errors"][0]["error"]["code"], "NATIVE_TURN_FAILED")
        self.assertEqual(result["status"]["capacity"]["used"], 0)
        self.assertEqual(list((self.pipeline.root / "candidates").glob("*.json")), [])

    async def crash_after_exit(self):
        with patch.object(self.pipeline, "finish", side_effect=fixtures.SimulatedControllerCrash("after exit")):
            with self.assertRaises(fixtures.SimulatedControllerCrash):
                await self.fixture.dispatch()
        task = self.fixture.ledger.task("fixture", "native")
        attempt = self.fixture.ledger.attempt(task["latest_attempt"])
        self.assertEqual(attempt["state"], "RELEASED")
        self.assertEqual(attempt["exit_code"], 0)
        return attempt

    async def test_recovery_reloads_executor_paths_and_truncation_evidence(self):
        self.add()
        attempt = await self.crash_after_exit()
        with patch.object(self.fixture.executor, "reconcile", wraps=self.fixture.executor.reconcile) as reconcile:
            self.assertEqual(await self.pipeline.recover_completed("fixture"), [])
            reconcile.assert_called_once_with(attempt["identity"])
        self.assertEqual(self.fixture.ledger.task("fixture", "native")["state"], "SUBMITTED")

    async def test_recovery_cannot_assume_missing_stdout_truncation_flag_is_false(self):
        self.add()
        attempt = await self.crash_after_exit()
        actual = self.fixture.executor.reconcile(attempt["identity"])
        actual.pop("log_truncated")
        with patch.object(self.fixture.executor, "reconcile", return_value=actual):
            errors = await self.pipeline.recover_completed("fixture")
        self.assertEqual(len(errors), 1)
        task = self.fixture.ledger.task("fixture", "native")
        self.assertEqual(task["state"], "BLOCKED")
        self.assertIsNone(task["candidate"])
        self.assertEqual(self.fixture.ledger.status()["capacity"]["used"], 0)


if __name__ == "__main__":
    unittest.main()
