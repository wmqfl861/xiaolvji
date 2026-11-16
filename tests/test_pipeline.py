"""Whole-pipeline tests using real SQLite, Git, supervisors and check commands.

Only the model generation command is replaced by a deterministic fixture. No
host CLI availability or real-model behavior is inferred from these tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, Principal, digest
from xiaolvji.dispatcher import Dispatcher
from xiaolvji.executor import ProcessExecutor
from xiaolvji.integration import integration_baseline
from xiaolvji.pipeline import Pipeline
from xiaolvji.authority import atomic_json as real_atomic_json
from xiaolvji.store import Ledger
from xiaolvji.workspace import artifact_digest, git


OPERATOR = Principal("fixture-operator", "operator")
PLANNER = Principal("fixture-planner", "planner")
REVIEWER = Principal("fixture-reviewer", "reviewer")


class SimulatedControllerCrash(BaseException):
    """Bypass normal exception recovery, as abrupt controller loss would."""


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src/baseline.txt").write_text("baseline\n")
        git("init", cwd=self.repo)
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "fixture baseline", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        self.state = self.root / "state"
        self.ledger = Ledger(self.state / "ledger.sqlite3", global_limit=2)
        self.ledger.enable_project("fixture", self.repo, {"project_limit": 2,
                    "profile": "cooperative_native", "review_authority": REVIEWER.id}, OPERATOR, "enable")
        scopes = [{"path": "src", "kind": "directory"}]
        self.plan = {"id": "fixture-plan", "revision": 1, "scope_digest": digest(scopes),
                     "approved_scope": scopes, "approval_ref": "fixture:explicit-test-approval", "contracts": []}
        self.ledger.register_plan("fixture", self.plan, PLANNER, "plan")
        self.commands = {}
        self.executor = ProcessExecutor(self.state / "executor")
        if not self.executor.probe()["supported"]:
            self.skipTest("Linux process integration capability is required")
        def command(attempt, workspace, prompt):
            return {"argv": [sys.executable, "-c", self.commands[attempt["task_id"]]],
                    "stdin_text": None, "env": {"PYTHONDONTWRITEBYTECODE": "1"}}
        self.pipeline = Pipeline(self.state / "pipeline", self.ledger, command_factory=command)
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def add(self, task_id, command, check, depends=None, scope=None, resources=None, retries=0):
        self.commands[task_id] = command
        task = {"schema_version": 1, "task_id": task_id,
                "plan": {key: self.plan[key] for key in ("id", "revision", "scope_digest")},
                "role": "worker", "engine": "codex", "objective": "Produce the fixture artifact " + task_id,
                "base_commit": self.base, "contract_versions": [], "depends_on": depends or [],
                "write_scope": scope or [{"path": "src/" + task_id + ".txt", "kind": "file"}],
                "exclusive_resources": resources or [], "acceptance": [{"id": "behavior", "criterion": "Exact fixture behavior is verified",
                    "argv": [sys.executable, "-c", check], "cwd": ".", "timeout_ms": 5000, "required": True}],
                "expected_artifacts": ["candidate_commit", "validation_report"],
                "retry": {"max_retries": retries, "backoff_ms": 0}, "final_review_required": True}
        self.ledger.register_task("fixture", task, PLANNER, "task:" + task_id)
        return task

    def simple(self, task_id, value, check_suffix="", depends=None, resources=None):
        command = f"from pathlib import Path; Path('src/{task_id}.txt').write_text({value!r})"
        check = f"from pathlib import Path; assert Path('src/{task_id}.txt').read_text() == {value!r}; " + check_suffix
        return self.add(task_id, command, check, depends, resources=resources)

    async def dispatch(self):
        dispatcher = Dispatcher(self.ledger, self.executor, self.pipeline, self.events.append)
        return await dispatcher.run("fixture", timeout=20)

    def originals(self):
        return [self.ledger.task("fixture", identifier) for identifier in self.commands]

    async def test_full_pipeline_preserves_verified_commit_and_requires_independent_review(self):
        self.simple("alpha", "alpha implementation", resources=["fixture:alpha-database"])
        self.simple("beta", "beta implementation", resources=["fixture:beta-database"])
        result = await self.dispatch()
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["status"]["capacity"]["used"])
        self.assertTrue(all(item["state"] == "AWAITING_REVIEW" for item in self.originals()))
        integrator_tasks = [item for item in result["status"]["tasks"] if item["task"]["role"] == "integrator"]
        self.assertEqual(1, len(integrator_tasks))
        self.assertTrue({"fixture:alpha-database", "fixture:beta-database"} <= set(integrator_tasks[0]["task"]["exclusive_resources"]),
                        "Combined checks must reserve every external resource required by their constituent checks")
        current = integration_baseline(self.repo)
        self.assertIsNotNone(current["expected_commit"])
        actual_commit = current["expected_commit"]
        actual_digest = artifact_digest(self.repo / ".git", actual_commit)
        self.assertEqual("alpha implementation", git("show", f"{actual_commit}:src/alpha.txt", cwd=self.repo))
        self.assertEqual("beta implementation", git("show", f"{actual_commit}:src/beta.txt", cwd=self.repo))
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))
        self.assertFalse((self.repo / "src/alpha.txt").exists())
        reports = [json.loads(path.read_text()) for path in (self.state / "pipeline/quality").glob("*/report.json")]
        self.assertEqual(3, len(reports))
        self.assertTrue(all(report["status"] == "passed" and report["source_unchanged"] for report in reports))
        integrated_reports = [report for report in reports if report["artifact"]["artifact_digest"] == actual_digest]
        self.assertTrue(any(report["artifact"]["commit"] == actual_commit for report in integrated_reports),
                        "The published commit must be the exact commit checked by integration verification")
        for task in self.originals():
            binding = {"plan": task["task"]["plan"], "decision": "approve", "review_record_id": "fixture-independent-review"}
            with self.assertRaises(Fault) as caught:
                self.ledger.review("fixture", task["task_id"], actual_digest, "approve", binding,
                                   Principal("worker", "worker"), "untrusted-review:" + task["task_id"])
            self.assertEqual("UNAUTHORIZED", caught.exception.code)
            self.ledger.review("fixture", task["task_id"], actual_digest, "approve", binding,
                               REVIEWER, "review:" + task["task_id"])
        self.assertTrue(all(item["state"] == "ACCEPTED" for item in self.originals()))

    async def test_nonzero_check_blocks_original_without_integration(self):
        self.add("alpha", "from pathlib import Path; Path('src/alpha.txt').write_text('wrong')",
                 "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'required'")
        result = await self.dispatch()
        target = self.ledger.task("fixture", "alpha")
        self.assertEqual("BLOCKED", target["state"])
        self.assertNotIn("implementation_verified", target["gates"])
        self.assertIsNone(integration_baseline(self.repo)["expected_commit"])
        self.assertEqual(0, result["status"]["capacity"]["used"])

    async def test_source_mutating_check_cannot_grant_technical_gate(self):
        self.simple("alpha", "correct", "Path('src/alpha.txt').write_text('mutation during check')")
        result = await self.dispatch()
        target = self.ledger.task("fixture", "alpha")
        self.assertEqual("BLOCKED", target["state"])
        self.assertNotIn("implementation_verified", target["gates"])
        self.assertIsNone(integration_baseline(self.repo)["expected_commit"])
        self.assertEqual(0, result["status"]["capacity"]["used"])

    async def test_check_temp_outputs_do_not_mutate_verifier_source_scope(self):
        self.simple("alpha", "correct", "import os; (Path(os.environ['TMPDIR']) / 'build-output').write_text('generated')")
        result = await self.dispatch()
        self.assertEqual([], result["errors"])
        self.assertEqual("AWAITING_REVIEW", self.ledger.task("fixture", "alpha")["state"])

    async def test_verified_dependency_is_materialized_before_downstream_work(self):
        self.simple("alpha", "upstream")
        self.add("beta", "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'upstream'; Path('src/beta.txt').write_text('downstream')",
                 "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'upstream'; assert Path('src/beta.txt').read_text() == 'downstream'",
                 [{"task_id": "alpha", "gate": "implementation_verified"}])
        result = await self.dispatch()
        self.assertEqual([], result["errors"])
        self.assertTrue(all(item["state"] == "AWAITING_REVIEW" for item in self.originals()))
        events = self.ledger.events(limit=1000)
        verify_alpha = next(event["seq"] for event in events if event["type"] == "gate_recorded" and event["task_id"] == "alpha" and event["payload"].get("gate") == "implementation_verified")
        admit_beta = next(event["seq"] for event in events if event["type"] == "run_reserved" and event["task_id"] == "beta")
        self.assertLess(verify_alpha, admit_beta)

    async def test_integrated_dependency_materializes_the_combined_delivery(self):
        self.simple("alpha", "upstream")
        self.simple("gamma", "combined-peer")
        first = await self.dispatch()
        self.assertEqual([], first["errors"])
        integrated = self.ledger.task("fixture", "alpha")["gates"]["integrated_verified"]["artifact_digest"]
        self.add("beta", "from pathlib import Path; assert Path('src/gamma.txt').read_text() == 'combined-peer'; Path('src/beta.txt').write_text('downstream')",
                 "from pathlib import Path; assert Path('src/gamma.txt').read_text() == 'combined-peer'; assert Path('src/beta.txt').read_text() == 'downstream'",
                 [{"task_id": "alpha", "gate": "integrated_verified", "artifact_digest": integrated}])
        second = await self.dispatch()
        self.assertEqual([], second["errors"])
        beta = self.ledger.task("fixture", "beta")
        self.assertEqual("AWAITING_REVIEW", beta["state"])

    async def test_initial_integrated_dependency_runs_after_verified_frontier(self):
        self.simple("alpha", "frontier")
        self.add("beta", "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'frontier'; Path('src/beta.txt').write_text('after integration')",
                 "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'frontier'; assert Path('src/beta.txt').read_text() == 'after integration'",
                 [{"task_id": "alpha", "gate": "integrated_verified"}])
        result = await self.dispatch()
        self.assertEqual([], result["errors"])
        self.assertTrue(all(item["state"] == "AWAITING_REVIEW" for item in self.originals()))
        events = self.ledger.events(limit=1000)
        first_integration = next(event["seq"] for event in events if event["type"] == "gate_recorded"
                                 and event["task_id"] == "alpha" and event["payload"].get("gate") == "integrated_verified")
        beta_admission = next(event["seq"] for event in events if event["type"] == "run_reserved" and event["task_id"] == "beta")
        self.assertLess(first_integration, beta_admission)
        delivery = integration_baseline(self.repo)["expected_commit"]
        self.assertEqual("after integration", git("show", f"{delivery}:src/beta.txt", cwd=self.repo))

    async def test_final_acceptance_dependency_pauses_until_configured_review(self):
        self.simple("alpha", "reviewed frontier")
        self.add("beta", "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'reviewed frontier'; Path('src/beta.txt').write_text('after review')",
                 "from pathlib import Path; assert Path('src/alpha.txt').read_text() == 'reviewed frontier'; assert Path('src/beta.txt').read_text() == 'after review'",
                 [{"task_id": "alpha", "gate": "final_accepted"}])
        paused = await self.dispatch()
        self.assertEqual([], paused["errors"])
        self.assertEqual(0, paused["status"]["capacity"]["used"])
        alpha = self.ledger.task("fixture", "alpha")
        self.assertEqual("AWAITING_REVIEW", alpha["state"])
        self.assertIsNone(self.ledger.task("fixture", "beta")["latest_attempt"])
        self.assertFalse(any(event["type"] == "run_reserved" and event["task_id"] == "beta"
                             for event in self.ledger.events(limit=1000)))
        approved_delivery = alpha["gates"]["integrated_verified"]["artifact_digest"]
        binding = {"plan": alpha["task"]["plan"], "decision": "approve",
                   "review_record_id": "fixture-only:configured-independent-review"}
        with self.assertRaises(Fault) as denied:
            self.ledger.review("fixture", "alpha", approved_delivery, "approve", binding, PLANNER, "wrong-reviewer")
        self.assertEqual("UNAUTHORIZED", denied.exception.code)
        # This is a test-only independent reviewer fixture, not self-approval
        # of this repository or a claim that production review has occurred.
        self.ledger.review("fixture", "alpha", approved_delivery, "approve", binding, REVIEWER, "fixture-frontier-review")
        resumed = await self.dispatch()
        self.assertEqual([], resumed["errors"])
        self.assertEqual("AWAITING_REVIEW", self.ledger.task("fixture", "beta")["state"])
        # The old approval authorizes the immutable input; the new combined
        # delivery still requires its own independent review.
        self.assertEqual("AWAITING_REVIEW", self.ledger.task("fixture", "alpha")["state"])
        events = self.ledger.events(limit=1000)
        approval = next(event["seq"] for event in events if event["type"] == "review_recorded" and event["task_id"] == "alpha")
        beta_admission = next(event["seq"] for event in events if event["type"] == "run_reserved" and event["task_id"] == "beta")
        self.assertLess(approval, beta_admission)
        delivery = integration_baseline(self.repo)["expected_commit"]
        self.assertEqual("after review", git("show", f"{delivery}:src/beta.txt", cwd=self.repo))

    async def test_strict_reservation_cannot_use_a_cooperative_executor(self):
        self.ledger.disable_project("fixture", OPERATOR, "profile-disable")
        self.ledger.enable_project("fixture", self.repo, {"project_limit": 2,
                    "profile": "controlled_executor", "review_authority": REVIEWER.id}, OPERATOR, "profile-strict")
        self.simple("alpha", "must never launch")
        attempt = self.ledger.reserve("fixture", "alpha", Principal("fixture-coordinator", "coordinator"), "strict-reserve")
        # This exercises the dispatch boundary reached both by run --all and
        # restored outbox entries, independent of CLI preflight selection.
        dispatcher = Dispatcher(self.ledger, self.executor, self.pipeline, self.events.append)
        await dispatcher._run_one(attempt)
        self.assertFalse((self.executor.state_dir / "launches" / attempt["attempt_id"]).exists())
        self.assertIsNone(self.ledger.task("fixture", "alpha")["candidate"])
        self.assertTrue(any(item["error"]["code"] == "CAPABILITY_UNSUPPORTED" for item in dispatcher.errors))

    async def test_disabled_project_outbox_cannot_start_a_worker(self):
        self.simple("alpha", "must never launch")
        attempt = self.ledger.reserve("fixture", "alpha", Principal("fixture-coordinator", "coordinator"), "disabled-reserve")
        self.ledger.disable_project("fixture", OPERATOR, "disable-after-reservation")
        dispatcher = Dispatcher(self.ledger, self.executor, self.pipeline, self.events.append)
        await dispatcher._run_one(attempt)
        self.assertFalse((self.executor.state_dir / "launches" / attempt["attempt_id"]).exists())
        self.assertIsNone(self.ledger.task("fixture", "alpha")["candidate"])
        self.assertFalse(any(event.get("type") == "started" for event in self.events))

    async def test_disable_during_workspace_preparation_prevents_process_start(self):
        self.simple("alpha", "must never launch")
        attempt = self.ledger.reserve("fixture", "alpha", Principal("fixture-coordinator", "coordinator"), "preparation-reserve")
        prepare = self.pipeline.prepare
        async def disable_after_prepare(current):
            job = await prepare(current)
            self.ledger.disable_project("fixture", OPERATOR, "disable-during-preparation")
            return job
        with patch.object(self.pipeline, "prepare", disable_after_prepare):
            dispatcher = Dispatcher(self.ledger, self.executor, self.pipeline, self.events.append)
            await dispatcher._run_one(attempt)
        self.assertFalse((self.executor.state_dir / "launches" / attempt["attempt_id"]).exists())
        self.assertIsNone(self.ledger.task("fixture", "alpha")["candidate"])
        self.assertFalse(any(event.get("type") == "started" for event in self.events))

    async def test_recipe_registration_crash_is_recovered(self):
        self.pipeline.auto_integrate = False
        self.simple("alpha", "upstream")
        initial = await self.dispatch()
        self.assertEqual([], initial["errors"])
        self.assertEqual("TECHNICALLY_VERIFIED", self.ledger.task("fixture", "alpha")["state"])
        self.pipeline.auto_integrate = True
        original_registration = self.ledger.register_task
        def interrupted_registration(project_id, task, principal, request_id):
            if task["task_id"].startswith("integrate-"):
                raise OSError("fixture crash between recipe persistence and ledger registration")
            return original_registration(project_id, task, principal, request_id)
        with patch.object(self.ledger, "register_task", interrupted_registration):
            with self.assertRaises(OSError):
                await self.pipeline.discover("fixture")
        # The next owned controller loop must reconcile this durable recipe;
        # absence of a task must not turn into a permanent skip marker.
        recovered = await self.dispatch()
        self.assertEqual([], recovered["errors"])
        self.assertEqual("AWAITING_REVIEW", self.ledger.task("fixture", "alpha")["state"])

    async def test_integration_conflict_is_visible_in_controller_status(self):
        scope = [{"path": "src/shared.txt", "kind": "file"}]
        for task_id, value in (("alpha", "one"), ("beta", "two")):
            self.add(task_id, f"from pathlib import Path; Path('src/shared.txt').write_text({value!r})",
                     f"from pathlib import Path; assert Path('src/shared.txt').read_text() == {value!r}", scope=scope)
        result = await self.dispatch()
        self.assertIsNone(integration_baseline(self.repo)["expected_commit"])
        visible = any(item.get("error", {}).get("code") == "INTEGRATION_CONFLICT" for item in result["errors"])
        visible = visible or any(item["state"] == "BLOCKED" and item["task"]["role"] == "integrator" and item.get("blocked_reason")
                                 for item in result["status"]["tasks"])
        self.assertTrue(visible, "An integration conflict must be visible through returned status or errors, not only a hidden recipe file")

    async def test_publication_receipt_crash_recovers_without_rerunning_workers(self):
        self.pipeline.auto_integrate = False
        self.simple("alpha", "verified")
        self.assertEqual([], (await self.dispatch())["errors"])
        self.pipeline.auto_integrate = True
        crashed = False
        def receipt_crash(path, value):
            nonlocal crashed
            path = Path(path)
            if (not crashed and path.parent.name == "deliveries" and
                    integration_baseline(self.repo)["expected_commit"] is not None):
                crashed = True
                raise SimulatedControllerCrash("fixture loss after Git CAS, before delivery receipt")
            return real_atomic_json(path, value)
        with patch("xiaolvji.pipeline.atomic_json", receipt_crash):
            with self.assertRaises(SimulatedControllerCrash):
                await self.dispatch()
        self.assertTrue(crashed)
        published = integration_baseline(self.repo)["expected_commit"]
        self.assertIsNotNone(published)
        reservations = sum(event["type"] == "run_reserved" for event in self.ledger.events(limit=1000))
        recovered = await self.dispatch()
        self.assertEqual([], recovered["errors"])
        self.assertEqual("AWAITING_REVIEW", self.ledger.task("fixture", "alpha")["state"])
        self.assertEqual(published, integration_baseline(self.repo)["expected_commit"])
        self.assertEqual(reservations, sum(event["type"] == "run_reserved" for event in self.ledger.events(limit=1000)),
                         "Crash recovery must finish the proven publication instead of relaunching work")

    async def test_timeout_zero_exit_cannot_pass_after_crash_before_failure_classification(self):
        self.add("timeout", "import signal,sys,time; from pathlib import Path; "
                 "signal.signal(signal.SIGTERM, lambda *args: sys.exit(0)); "
                 "Path('src/timeout.txt').write_text('incomplete'); time.sleep(30)",
                 "raise RuntimeError('a timed out implementation must never be checked')", retries=1)
        prepare = self.pipeline.prepare
        async def bounded_prepare(attempt):
            return {**await prepare(attempt), "timeout": 0.3}
        with patch.object(self.pipeline, "prepare", bounded_prepare), patch.object(
                self.pipeline, "failed", side_effect=SimulatedControllerCrash("after exit, before failure classification")):
            with self.assertRaises(SimulatedControllerCrash):
                await self.dispatch()
        original = self.ledger.task("fixture", "timeout")
        attempt = self.ledger.attempt(original["latest_attempt"])
        self.assertEqual(0, attempt["exit_code"], "The process deliberately handles SIGTERM with exit zero")
        self.assertTrue(attempt["outcome"]["timed_out"])
        reservations = sum(e["type"] == "run_reserved" for e in self.ledger.events(limit=1000))
        recovered = await self.dispatch()
        self.assertEqual([], recovered["errors"])
        self.assertEqual("BLOCKED", self.ledger.task("fixture", "timeout")["state"])
        self.assertIsNone(self.ledger.task("fixture", "timeout")["candidate"])
        self.assertEqual(reservations, sum(e["type"] == "run_reserved" for e in self.ledger.events(limit=1000)))

    async def test_nonzero_crash_gap_blocks_before_automatic_retry_admission(self):
        self.add("failed", "raise SystemExit(7)", "raise RuntimeError('must not run')", retries=1)
        with patch.object(self.pipeline, "failed", side_effect=SimulatedControllerCrash("failure handling interrupted")):
            with self.assertRaises(SimulatedControllerCrash):
                await self.dispatch()
        reservations = sum(e["type"] == "run_reserved" for e in self.ledger.events(limit=1000))
        recovered = await self.dispatch()
        self.assertEqual([], recovered["errors"])
        self.assertEqual("BLOCKED", self.ledger.task("fixture", "failed")["state"])
        self.assertEqual(reservations, sum(e["type"] == "run_reserved" for e in self.ledger.events(limit=1000)))


if __name__ == "__main__":
    unittest.main()
