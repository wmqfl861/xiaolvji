"""Ledger invariant tests using actual SQLite locks and independent connections."""
from __future__ import annotations

import concurrent.futures
import copy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from xiaolvji.contracts import Fault, Principal, digest
from xiaolvji.store import Ledger

OP = Principal("operator-1", "operator")
PLAN = Principal("planner-1", "planner")
COORD = Principal("coordinator-1", "coordinator")
EXEC = Principal("executor-1", "executor")
VERIFY = Principal("verifier-1", "verifier")
INTEGRATE = Principal("integrator-1", "integrator")
REVIEW = Principal("reviewer-1", "reviewer")
WORKER = Principal("unbound-worker", "worker")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.path = self.home / "state" / "controller.sqlite3"
        self.ledger = Ledger(self.path, global_limit=2)
        self.counter = 0
        self.project("project", self.repo)
        self.plan = self.make_plan()
        self.ledger.register_plan("project", self.plan, PLAN, self.key())

    def key(self):
        self.counter += 1
        return "req-" + str(self.counter)

    def project(self, project_id, root, limit=2):
        root.mkdir(exist_ok=True)
        return self.ledger.enable_project(project_id, str(root), {
            "project_limit": limit, "profile": "cooperative_native", "review_authority": REVIEW.id,
        }, OP, self.key())

    @staticmethod
    def make_plan(revision=1):
        scopes = [{"path": "src", "kind": "directory"}, {"path": "tests", "kind": "directory"}]
        return {"id": "approved-plan", "revision": revision, "scope_digest": digest(scopes),
                "approved_scope": scopes, "approval_ref": "fixture:operator-approved-requirements", "contracts": []}

    def definition(self, task_id, scope=None, depends=None, retries=0, project_id="project"):
        plan = self.ledger.project(project_id)["plan"]
        return {"schema_version": 1, "task_id": task_id,
                "plan": {key: plan[key] for key in ("id", "revision", "scope_digest")},
                "role": "worker", "engine": "codex", "objective": "Implement and verify " + task_id,
                "base_commit": "1" * 40, "contract_versions": [], "depends_on": depends or [],
                "write_scope": scope if scope is not None else [{"path": "src/" + task_id + ".py", "kind": "file"}],
                "exclusive_resources": [], "acceptance": [{"id": "check", "criterion": "All stated behavior passes"}],
                "expected_artifacts": ["candidate_commit"], "retry": {"max_retries": retries, "backoff_ms": 0},
                "final_review_required": True}

    def add(self, task_id, **kwargs):
        definition = self.definition(task_id, **kwargs)
        self.ledger.register_task(kwargs.get("project_id", "project"), definition, PLAN, self.key())
        return definition

    def launch(self, task_id, project_id="project"):
        attempt = self.ledger.reserve(project_id, task_id, COORD, self.key())
        self.ledger.claim_launch(attempt["attempt_id"], attempt["generation"], EXEC, self.key())
        identity = {"pid": 1000 + self.counter, "start_ticks": str(self.counter), "boot_id": "test-boot"}
        self.ledger.observe(attempt["attempt_id"], attempt["generation"], "started", {"identity": identity}, EXEC, self.key())
        return self.ledger.attempt(attempt["attempt_id"])

    def finish(self, attempt, code=0, terminated=True, request_id=None):
        return self.ledger.observe(attempt["attempt_id"], attempt["generation"], "exited", {
            "identity": attempt["identity"], "exit_code": code, "process_tree_terminated": terminated,
        }, EXEC, request_id or self.key())

    def candidate(self, task_id, seed="candidate", depends=None):
        if not any(task["task_id"] == task_id for task in self.ledger.status()["tasks"]):
            self.add(task_id, depends=depends)
        attempt = self.launch(task_id)
        self.finish(attempt)
        definition = attempt["task"]
        candidate = {"commit": "2" * 40, "artifact_digest": digest(seed), "plan": definition["plan"],
                     "changed_paths": ["src/" + task_id + ".py"]}
        self.ledger.submit_candidate(attempt["attempt_id"], attempt["generation"], candidate, EXEC, self.key())
        return attempt, candidate

    def gate(self, task_id, name, artifact, **extras):
        roles = {"contract_ready": PLAN, "implementation_verified": VERIFY,
                 "integrated_verified": INTEGRATE, "final_accepted": REVIEW}
        binding = {"plan": self.ledger.task("project", task_id)["task"]["plan"], **extras}
        return self.ledger.record_gate("project", task_id, name, artifact, binding, roles[name], self.key())

    def assertFault(self, expected_code, callable_, *args, **kwargs):
        with self.assertRaises(Fault) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected_code)
        return caught.exception

    def test_timeout_or_cancelled_zero_exit_cannot_submit_candidate(self):
        for flag in ("timed_out", "cancelled"):
            with self.subTest(flag=flag):
                self.add(flag, retries=1)
                attempt = self.launch(flag)
                outcome = {"identity": attempt["identity"], "exit_code": 0, "process_tree_terminated": True, flag: True}
                self.ledger.observe(attempt["attempt_id"], attempt["generation"], "exited", outcome, EXEC, self.key())
                self.assertTrue(self.ledger.attempt(attempt["attempt_id"])["outcome"][flag])
                candidate = {"commit": "2" * 40, "artifact_digest": digest("unfinished"),
                             "plan": attempt["task"]["plan"], "changed_paths": []}
                self.assertFault("INSTANCE_UNCERTAIN", self.ledger.submit_candidate,
                                 attempt["attempt_id"], attempt["generation"], candidate, EXEC, self.key())
                self.assertIsNone(self.ledger.task("project", flag)["candidate"])
        self.assertEqual(0, self.ledger.status()["capacity"]["used"])

    def test_atomic_capacity_across_independent_connections(self):
        for index in range(12):
            self.add("task" + str(index))
        barrier = threading.Barrier(12)
        def reserve(index):
            other = Ledger(self.path)
            barrier.wait()
            try:
                return other.reserve("project", "task" + str(index), COORD, "concurrent-" + str(index))["state"]
            except Fault as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(reserve, range(12)))
        self.assertEqual(outcomes.count("RESERVED"), 2)
        self.assertEqual(outcomes.count("CAPACITY_FULL"), 10)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 2)
        self.assertEqual(len(self.ledger.launches()), 2)

    def test_global_pool_shared_between_projects(self):
        self.project("other", self.home / "other", limit=2)
        self.ledger.register_plan("other", self.make_plan(), PLAN, self.key())
        self.add("a")
        self.add("b", project_id="other")
        self.add("c", project_id="other")
        self.ledger.reserve("project", "a", COORD, self.key())
        self.ledger.reserve("other", "b", COORD, self.key())
        self.assertFault("CAPACITY_FULL", self.ledger.reserve, "other", "c", COORD, self.key())
        self.assertEqual(self.ledger.status("other")["capacity"]["used"], 2)

    def test_project_budget_can_be_stricter_than_global(self):
        self.project("small", self.home / "small", limit=1)
        self.ledger.register_plan("small", self.make_plan(), PLAN, self.key())
        self.add("a", project_id="small")
        self.add("b", project_id="small")
        self.ledger.reserve("small", "a", COORD, self.key())
        self.assertFault("CAPACITY_FULL", self.ledger.reserve, "small", "b", COORD, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["available"], 1)

    def test_directory_file_and_resource_conflicts(self):
        self.add("directory", scope=[{"path": "src/shared", "kind": "directory"}])
        self.add("file", scope=[{"path": "src/shared/module.py", "kind": "file"}])
        self.add("independent")
        self.ledger.reserve("project", "directory", COORD, self.key())
        self.assertFault("SCOPE_CONFLICT", self.ledger.reserve, "project", "file", COORD, self.key())
        self.ledger.reserve("project", "independent", COORD, self.key())

    def test_exclusive_resources_conflict_across_projects(self):
        self.project("other", self.home / "other")
        self.ledger.register_plan("other", self.make_plan(), PLAN, self.key())
        for project_id in ("project", "other"):
            definition = self.definition("database", project_id=project_id)
            definition["exclusive_resources"] = ["database:shared-test-db"]
            self.ledger.register_task(project_id, definition, PLAN, self.key())
        self.ledger.reserve("project", "database", COORD, self.key())
        self.assertFault("SCOPE_CONFLICT", self.ledger.reserve, "other", "database", COORD, self.key())

    def test_idempotency_returns_same_attempt_and_binds_principal(self):
        self.add("a")
        attempt = self.ledger.reserve("project", "a", COORD, "reserve-once")
        again = self.ledger.reserve("project", "a", COORD, "reserve-once")
        self.assertEqual(attempt, again)
        self.assertEqual(len(self.ledger.launches()), 1)
        self.assertFault("IDEMPOTENCY_CONFLICT", self.ledger.reserve, "project", "a", Principal("different", "coordinator"), "reserve-once")
        self.add("b")
        self.assertFault("IDEMPOTENCY_CONFLICT", self.ledger.reserve, "project", "b", COORD, "reserve-once")

    def test_outbox_claim_is_not_a_second_launch(self):
        self.add("a")
        attempt = self.ledger.reserve("project", "a", COORD, self.key())
        self.assertEqual(len(Ledger(self.path).launches()), 1)
        self.ledger.claim_launch(attempt["attempt_id"], 1, EXEC, "claim-once")
        self.assertEqual(Ledger(self.path).launches(), [])
        self.assertEqual(self.ledger.claim_launch(attempt["attempt_id"], 1, EXEC, "claim-once")["state"], "STARTING")
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.claim_launch, attempt["attempt_id"], 1, EXEC, "claim-again")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)

    def test_lost_and_unconfirmed_exit_do_not_release_capacity(self):
        self.add("a")
        attempt = self.launch("a")
        self.ledger.observe(attempt["attempt_id"], 1, "lost", {"reason": "transport disconnected"}, EXEC, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["uncertain"], 1)
        self.assertFault("INSTANCE_UNCERTAIN", self.finish, attempt, terminated=False)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.finish(attempt)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)

    def test_unknown_or_changed_process_identity_cannot_release(self):
        self.add("a")
        attempt = self.launch("a")
        wrong = copy.deepcopy(attempt)
        wrong["identity"]["start_ticks"] = "different-process"
        self.assertFault("UNKNOWN_IDENTITY", self.finish, wrong)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.observe, attempt["attempt_id"], 1, "start_failed",
                         {"no_process_created": True, "reason": "cannot assert this after acknowledging a process"}, EXEC, self.key())

    def test_terminal_events_release_exactly_once(self):
        self.add("a")
        attempt = self.launch("a")
        first = self.finish(attempt, request_id="exit-first")
        same = self.finish(attempt, request_id="exit-second")
        self.assertEqual(first, same)
        releases = [event for event in self.ledger.events() if event["type"] == "resource_released"]
        self.assertEqual(len(releases), 1)
        self.assertFault("REVISION_CONFLICT", self.finish, attempt, code=1)

    def test_retry_and_stale_generation(self):
        self.add("a", retries=1)
        first = self.launch("a")
        self.finish(first, code=1)
        self.assertEqual(self.ledger.task("project", "a")["state"], "READY")
        second = self.launch("a")
        self.assertEqual(second["generation"], 2)
        self.assertFault("REVISION_CONFLICT", self.finish, first)
        self.finish(second, code=1)
        self.assertEqual(self.ledger.task("project", "a")["blocked_reason"], "RETRY_EXHAUSTED")
        self.assertEqual(self.ledger.ready_tasks(), [])

    def test_start_failure_can_release_only_before_process_identity(self):
        self.add("a")
        attempt = self.ledger.reserve("project", "a", COORD, self.key())
        self.ledger.claim_launch(attempt["attempt_id"], 1, EXEC, self.key())
        self.ledger.observe(attempt["attempt_id"], 1, "start_failed", {"no_process_created": True, "reason": "missing executable"}, EXEC, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)
        self.assertEqual(self.ledger.task("project", "a")["state"], "BLOCKED")

    def test_cancellation_stops_queue_then_waits_for_exit(self):
        self.add("a")
        self.add("b")
        attempt = self.launch("a")
        cancelled = self.ledger.cancel("project", None, COORD, self.key())
        self.assertEqual(cancelled["attempts"][0]["state"], "STOPPING")
        self.assertEqual(cancelled["attempts"][0]["launch_state"], "CLAIMED")
        self.assertEqual(self.ledger.ready_tasks(), [])
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.finish(attempt)
        self.assertEqual(self.ledger.task("project", "a")["state"], "CANCELLED")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)

    def test_cancel_reserved_cannot_be_claimed(self):
        self.add("a")
        attempt = self.ledger.reserve("project", "a", COORD, self.key())
        self.ledger.cancel("project", "a", COORD, self.key())
        self.assertEqual(self.ledger.launches(), [])
        self.assertEqual(self.ledger.attempt(attempt["attempt_id"])["launch_state"], "CANCELLED")
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.claim_launch, attempt["attempt_id"], 1, EXEC, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.ledger.observe(attempt["attempt_id"], 1, "start_failed", {"no_process_created": True, "reason": "cancelled before launch claim"}, EXEC, self.key())
        self.assertEqual(self.ledger.task("project", "a")["state"], "CANCELLED")

    def test_forward_dependencies_and_cycles(self):
        self.add("consumer", depends=[{"task_id": "producer", "gate": "contract_ready"}])
        self.assertEqual(self.ledger.ready_tasks(), [])
        cyclic = self.definition("producer", depends=[{"task_id": "consumer", "gate": "contract_ready"}])
        self.assertFault("DEPENDENCY_CYCLE", self.ledger.register_task, "project", cyclic, PLAN, self.key())
        self.add("producer")
        contract_digest = digest("frozen contract")
        self.gate("producer", "contract_ready", contract_digest)
        self.assertIn("consumer", [task["task_id"] for task in self.ledger.ready_tasks()])

    def test_dependency_snapshot_invalidates_inflight_candidate(self):
        self.add("producer")
        self.gate("producer", "contract_ready", digest("v1"))
        self.add("consumer", depends=[{"task_id": "producer", "gate": "contract_ready"}])
        attempt = self.launch("consumer")
        self.gate("producer", "contract_ready", digest("v2"))
        self.finish(attempt)
        self.assertFault("CONTRACT_STALE", self.ledger.submit_candidate, attempt["attempt_id"], 1,
                         {"commit": "2" * 40, "artifact_digest": digest("candidate"), "plan": attempt["task"]["plan"]}, EXEC, self.key())

    def test_plan_revision_invalidates_tasks_and_cannot_race_active_work(self):
        self.add("a")
        attempt = self.launch("a")
        revised = self.make_plan(2)
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.register_plan, "project", revised, PLAN, self.key())
        self.finish(attempt)
        self.ledger.register_plan("project", revised, PLAN, self.key())
        self.assertEqual(self.ledger.task("project", "a")["state"], "BLOCKED")
        self.assertEqual(self.ledger.ready_tasks(), [])
        stale = self.definition("b")
        stale["plan"]["revision"] = 1
        self.assertFault("CONTRACT_STALE", self.ledger.register_task, "project", stale, PLAN, self.key())

    def test_scope_and_contract_approval_checks(self):
        out = self.definition("outside", scope=[{"path": "unapproved.py", "kind": "file"}])
        self.assertFault("SCOPE_CONFLICT", self.ledger.register_task, "project", out, PLAN, self.key())
        bad_contract = self.definition("contract")
        bad_contract["contract_versions"] = [{"id": "api", "version": "1", "digest": digest("api")}]
        self.assertFault("CONTRACT_STALE", self.ledger.register_task, "project", bad_contract, PLAN, self.key())
        wrong_plan = self.make_plan(2)
        wrong_plan["scope_digest"] = digest("some other scope")
        self.assertFault("REVISION_CONFLICT", self.ledger.register_plan, "project", wrong_plan, PLAN, self.key())

    def test_schema_invalid_task_and_changed_identity_rejected(self):
        malformed = self.definition("malformed")
        malformed["retry"]["max_retries"] = 3
        self.assertFault("INVALID_TASK", self.ledger.register_task, "project", malformed, PLAN, self.key())
        definition = self.add("a")
        definition["objective"] = "New objective hidden behind old task id"
        self.assertFault("REVISION_CONFLICT", self.ledger.register_task, "project", definition, PLAN, self.key())

    def test_technical_verification_integration_and_independent_review(self):
        attempt, candidate = self.candidate("a")
        self.assertFault("UNAUTHORIZED", self.ledger.record_gate, "project", "a", "implementation_verified", candidate["artifact_digest"],
                         {"plan": candidate["plan"]}, WORKER, self.key())
        self.assertFault("CONTRACT_STALE", self.gate, "a", "implementation_verified", digest("other"))
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("integrated tree")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        self.assertEqual(self.ledger.task("project", "a")["state"], "AWAITING_REVIEW")
        self.assertFault("UNAUTHORIZED", self.ledger.record_gate, "project", "a", "final_accepted", delivery,
                         {"plan": candidate["plan"], "review_record_id": "external-review-1"}, Principal("wrong-reviewer", "reviewer"), self.key())
        self.assertFault("CONTRACT_STALE", self.gate, "a", "final_accepted", digest("other delivery"), review_record_id="external-review-1")
        self.gate("a", "final_accepted", delivery, review_record_id="external-review-1")
        self.assertEqual(self.ledger.task("project", "a")["state"], "ACCEPTED")

    def test_repeated_same_verification_does_not_downgrade_acceptance(self):
        attempt, candidate = self.candidate("a")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("integrated tree")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        self.gate("a", "final_accepted", delivery, review_record_id="review-1")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        self.assertEqual(self.ledger.task("project", "a")["state"], "ACCEPTED")

    def test_replacing_candidate_invalidates_verification_and_review(self):
        attempt, candidate = self.candidate("a")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("integrated")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        self.gate("a", "final_accepted", delivery, review_record_id="review-1")
        changed = {**candidate, "commit": "3" * 40, "artifact_digest": digest("new candidate")}
        self.ledger.submit_candidate(attempt["attempt_id"], 1, changed, EXEC, self.key())
        state = self.ledger.task("project", "a")
        self.assertEqual(state["state"], "SUBMITTED")
        self.assertEqual(state["gates"], {})

    def test_upstream_gate_change_invalidates_downstream_acceptance(self):
        self.add("producer")
        self.gate("producer", "contract_ready", digest("v1"))
        attempt, candidate = self.candidate("consumer", depends=[{"task_id": "producer", "gate": "contract_ready"}])
        self.gate("consumer", "implementation_verified", candidate["artifact_digest"])
        self.gate("consumer", "integrated_verified", digest("delivery"), candidate_digest=candidate["artifact_digest"])
        self.gate("consumer", "final_accepted", digest("delivery"), review_record_id="review-1")
        self.gate("producer", "contract_ready", digest("v2"))
        state = self.ledger.task("project", "consumer")
        self.assertEqual(state["state"], "BLOCKED")
        self.assertEqual(state["gates"], {})

    def test_stale_schema_and_config_do_not_silently_mutate_authority(self):
        self.assertFault("REVISION_CONFLICT", Ledger, self.path, global_limit=99)
        with self.assertRaises(Fault):
            self.project("nested", self.repo / "nested")
        self.assertFault("SCOPE_CONFLICT", self.ledger.enable_project, "state-project", str(self.path.parent),
                         {"project_limit": 2, "profile": "cooperative_native", "review_authority": REVIEW.id}, OP, self.key())
        self.assertEqual(Ledger(self.path).status()["capacity"]["limit"], 2)

    def test_block_task_requires_exit_and_invalidates_quality_gates(self):
        self.add("a")
        attempt = self.launch("a")
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.block_task, "project", "a", "SCOPE_VIOLATION", COORD, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.finish(attempt)
        candidate = {"commit": "2" * 40, "artifact_digest": digest("candidate"), "plan": attempt["task"]["plan"]}
        self.ledger.submit_candidate(attempt["attempt_id"], 1, candidate, EXEC, self.key())
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        self.ledger.block_task("project", "a", "REQUIRED_CHECK_FAILED", VERIFY, self.key())
        task = self.ledger.task("project", "a")
        self.assertEqual(task["state"], "BLOCKED")
        self.assertEqual(task["blocked_reason"], "REQUIRED_CHECK_FAILED")
        self.assertEqual(task["gates"], {})
        self.assertEqual(self.ledger.ready_tasks(), [])
        self.assertFault("UNAUTHORIZED", self.ledger.block_task, "project", "a", "self blocker", WORKER, self.key())

    def test_reviewer_reject_and_rework_require_exact_authority_and_delivery(self):
        attempt, candidate = self.candidate("a")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("integrated")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        binding = {"plan": candidate["plan"], "review_record_id": "external-review-reject"}
        self.assertFault("UNAUTHORIZED", self.ledger.review, "project", "a", delivery, "reject", binding,
                         Principal("other-reviewer", "reviewer"), self.key())
        self.assertFault("CONTRACT_STALE", self.ledger.review, "project", "a", digest("wrong-delivery"), "reject", binding, REVIEW, self.key())
        result = self.ledger.review("project", "a", delivery, "reject", binding, REVIEW, "reject-once")
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["blocked_reason"], "REVIEW_REJECTED")
        self.assertEqual(self.ledger.review("project", "a", delivery, "reject", binding, REVIEW, "reject-once"), result)
        rework = self.ledger.review("project", "a", delivery, "rework",
                                  {"plan": candidate["plan"], "review_record_id": "external-review-rework"}, REVIEW, self.key())
        self.assertEqual(rework["blocked_reason"], "REWORK_REQUIRED")
        self.assertEqual(self.ledger.ready_tasks(), [])
        self.assertEqual(rework["candidate"], candidate)
        self.assertEqual(rework["generation"], 1)
        self.assertTrue(any(event["type"] == "review_recorded" and event["payload"].get("decision") == "rework" for event in self.ledger.events()))

    def test_rework_cannot_be_reopened_by_candidate_or_technical_report(self):
        attempt, candidate = self.candidate("a")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("integrated")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        self.ledger.review("project", "a", delivery, "rework",
                           {"plan": candidate["plan"], "review_record_id": "review-rework"}, REVIEW, self.key())
        replacement = {**candidate, "artifact_digest": digest("new code")}
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.submit_candidate,
                         attempt["attempt_id"], 1, replacement, EXEC, self.key())
        self.assertFault("DEPENDENCY_BLOCKED", self.gate, "a", "implementation_verified", candidate["artifact_digest"])
        self.assertEqual(self.ledger.task("project", "a")["blocked_reason"], "REWORK_REQUIRED")

    def test_review_approval_and_later_rejection_clear_acceptance(self):
        attempt, candidate = self.candidate("a")
        self.gate("a", "implementation_verified", candidate["artifact_digest"])
        delivery = digest("delivery")
        self.gate("a", "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        result = self.ledger.review("project", "a", delivery, "approve",
                                    {"plan": candidate["plan"], "review_record_id": "external-approval"}, REVIEW, self.key())
        self.assertEqual(result["state"], "ACCEPTED")
        result = self.ledger.review("project", "a", delivery, "reject",
                                    {"plan": candidate["plan"], "review_record_id": "external-retraction"}, REVIEW, self.key())
        self.assertEqual(result["state"], "BLOCKED")
        self.assertNotIn("final_accepted", result["gates"])
        self.assertIn("integrated_verified", result["gates"])

    def test_claim_requires_enabled_project_and_retains_paused_reservation(self):
        self.add("a")
        attempt = self.ledger.reserve("project", "a", COORD, self.key())
        self.ledger.disable_project("project", OP, self.key())
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.claim_launch, attempt["attempt_id"], 1, EXEC, self.key())
        current = self.ledger.attempt(attempt["attempt_id"])
        self.assertEqual(current["state"], "RESERVED")
        self.assertEqual(current["launch_state"], "PENDING")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        config = self.ledger.project("project")["config"]
        self.ledger.enable_project("project", str(self.repo), config, OP, self.key())
        self.assertEqual(self.ledger.claim_launch(attempt["attempt_id"], 1, EXEC, self.key())["state"], "STARTING")

    def integrated_source(self, task_id="producer", depends=None):
        attempt, candidate = self.candidate(task_id, depends=depends)
        self.gate(task_id, "implementation_verified", candidate["artifact_digest"])
        delivery = digest("old-integrated-" + task_id)
        self.gate(task_id, "integrated_verified", delivery, candidate_digest=candidate["artifact_digest"])
        return attempt, candidate, delivery

    def test_explicit_integrated_pin_resolves_immutable_history(self):
        attempt, candidate, old = self.integrated_source()
        newer = digest("new combined delivery")
        self.gate("producer", "integrated_verified", newer, candidate_digest=candidate["artifact_digest"])
        old_gate = self.ledger.gate("project", "producer", "integrated_verified", old)
        self.assertTrue(old_gate["historical"])
        self.assertEqual(self.ledger.gate("project", "producer", "integrated_verified")["artifact_digest"], newer)
        self.add("consumer", depends=[{"task_id": "producer", "gate": "integrated_verified", "artifact_digest": old}])
        reserved = self.ledger.reserve("project", "consumer", COORD, self.key())
        self.assertEqual(reserved["dependency_snapshot"][0]["artifact_digest"], old)

    def test_started_unpinned_task_retains_consumed_integration_but_new_task_selects_current(self):
        attempt, candidate, old = self.integrated_source()
        self.add("consumer", depends=[{"task_id": "producer", "gate": "integrated_verified"}])
        consumed = self.launch("consumer")
        self.assertEqual(consumed["dependency_snapshot"][0]["artifact_digest"], old)
        newer = digest("newer integration")
        self.gate("producer", "integrated_verified", newer, candidate_digest=candidate["artifact_digest"])
        self.finish(consumed)
        produced = {"commit": "3" * 40, "artifact_digest": digest("consumer"), "plan": consumed["task"]["plan"], "changed_paths": ["src/consumer.py"]}
        self.ledger.submit_candidate(consumed["attempt_id"], 1, produced, EXEC, self.key())
        self.gate("consumer", "implementation_verified", produced["artifact_digest"])
        self.gate("consumer", "integrated_verified", newer, candidate_digest=produced["artifact_digest"])
        self.assertEqual(self.ledger.task("project", "consumer")["state"], "AWAITING_REVIEW")
        self.add("fresh", depends=[{"task_id": "producer", "gate": "integrated_verified"}])
        fresh = self.ledger.reserve("project", "fresh", COORD, self.key())
        self.assertEqual(fresh["dependency_snapshot"][0]["artifact_digest"], newer)

    def test_historical_review_proves_only_exact_old_delivery(self):
        attempt, candidate, old = self.integrated_source()
        self.gate("producer", "final_accepted", old, review_record_id="approved-old-delivery")
        self.add("consumer", depends=[{"task_id": "producer", "gate": "final_accepted"}])
        consumed = self.launch("consumer")
        newer = digest("new delivery requiring new review")
        self.gate("producer", "integrated_verified", newer, candidate_digest=candidate["artifact_digest"])
        self.assertEqual(self.ledger.task("project", "producer")["state"], "AWAITING_REVIEW")
        self.assertNotIn("final_accepted", self.ledger.task("project", "producer")["gates"])
        historical = self.ledger.gate("project", "producer", "final_accepted", old)
        self.assertTrue(historical["historical"])
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", "final_accepted", newer)
        self.finish(consumed)
        produced = {"commit": "3" * 40, "artifact_digest": digest("consumer"), "plan": consumed["task"]["plan"]}
        self.ledger.submit_candidate(consumed["attempt_id"], 1, produced, EXEC, self.key())
        self.gate("consumer", "implementation_verified", produced["artifact_digest"])
        self.add("fresh", depends=[{"task_id": "producer", "gate": "final_accepted"}])
        self.assertNotIn("fresh", [task["task_id"] for task in self.ledger.ready_tasks()])

    def test_reviewer_rework_revokes_old_delivery_and_review_history(self):
        attempt, candidate, old = self.integrated_source()
        self.gate("producer", "final_accepted", old, review_record_id="old-approval")
        newer = digest("new delivery")
        self.gate("producer", "integrated_verified", newer, candidate_digest=candidate["artifact_digest"])
        self.assertTrue(self.ledger.gate("project", "producer", "final_accepted", old)["historical"])
        self.ledger.review("project", "producer", newer, "rework",
                           {"plan": candidate["plan"], "review_record_id": "new-rework"}, REVIEW, self.key())
        for gate in ("integrated_verified", "final_accepted"):
            self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", gate, old)
        self.ledger.review("project", "producer", newer, "approve",
                           {"plan": candidate["plan"], "review_record_id": "new-approved-after-review"}, REVIEW, self.key())
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", "final_accepted", old)
        self.assertEqual(self.ledger.gate("project", "producer", "final_accepted", newer)["artifact_digest"], newer)

    def test_candidate_change_revokes_history_even_after_reverification(self):
        attempt, candidate, old = self.integrated_source()
        self.gate("producer", "integrated_verified", digest("second"), candidate_digest=candidate["artifact_digest"])
        changed = {**candidate, "commit": "4" * 40, "artifact_digest": digest("replacement code")}
        self.ledger.submit_candidate(attempt["attempt_id"], 1, changed, EXEC, self.key())
        self.gate("producer", "implementation_verified", changed["artifact_digest"])
        self.gate("producer", "integrated_verified", digest("third"), candidate_digest=changed["artifact_digest"])
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", "integrated_verified", old)

    def test_historical_integration_still_checks_consumed_contract_closure(self):
        self.add("contract")
        self.gate("contract", "contract_ready", digest("v1"))
        attempt, candidate, old = self.integrated_source(depends=[{"task_id": "contract", "gate": "contract_ready"}])
        self.gate("producer", "integrated_verified", digest("next delivery"), candidate_digest=candidate["artifact_digest"])
        self.assertTrue(self.ledger.gate("project", "producer", "integrated_verified", old)["historical"])
        self.gate("contract", "contract_ready", digest("v2"))
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", "integrated_verified", old)

    def test_review_authority_change_revokes_historical_reviews(self):
        attempt, candidate, old = self.integrated_source()
        self.gate("producer", "final_accepted", old, review_record_id="old-approved")
        self.gate("producer", "integrated_verified", digest("next"), candidate_digest=candidate["artifact_digest"])
        self.ledger.disable_project("project", OP, self.key())
        config = {**self.ledger.project("project")["config"], "review_authority": "replacement-reviewer"}
        self.ledger.enable_project("project", str(self.repo), config, OP, self.key())
        self.assertFault("DEPENDENCY_BLOCKED", self.ledger.gate, "project", "producer", "final_accepted", old)

    def test_publication_lease_blocks_plan_revision_without_worker_slot(self):
        binding = {key: self.plan[key] for key in ("id", "revision", "scope_digest")}
        lease = self.ledger.begin_publication("project", "publication-1", binding, INTEGRATE, self.key())
        status = self.ledger.status()
        self.assertEqual(status["capacity"]["used"], 0)
        self.assertEqual(status["pending_publications"], [lease])
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.register_plan, "project", self.make_plan(2), PLAN, self.key())
        self.assertEqual(Ledger(self.path).status()["pending_publications"], [lease])
        self.ledger.end_publication("project", "publication-1", INTEGRATE, self.key())
        self.ledger.register_plan("project", self.make_plan(2), PLAN, self.key())
        self.assertEqual(self.ledger.status()["pending_publications"], [])

    def test_publication_reentrant_owner_and_competing_writer_rules(self):
        binding = {key: self.plan[key] for key in ("id", "revision", "scope_digest")}
        first = self.ledger.begin_publication("project", "publication-1", binding, INTEGRATE, "publication-start")
        self.assertEqual(self.ledger.begin_publication("project", "publication-1", binding, INTEGRATE, "publication-start"), first)
        self.assertEqual(self.ledger.begin_publication("project", "publication-1", binding, INTEGRATE, self.key()), first)
        self.assertFault("SCOPE_CONFLICT", self.ledger.begin_publication, "project", "publication-2", binding, INTEGRATE, self.key())
        self.assertFault("UNAUTHORIZED", self.ledger.begin_publication, "project", "publication-1", binding,
                         Principal("other-integrator", "integrator"), self.key())
        self.assertFault("SCOPE_CONFLICT", self.ledger.end_publication, "project", "publication-2", INTEGRATE, self.key())
        self.assertFault("UNAUTHORIZED", self.ledger.end_publication, "project", "publication-1",
                         Principal("other-integrator", "integrator"), self.key())
        self.assertEqual(len(self.ledger.status()["pending_publications"]), 1)

    def test_publication_prevents_policy_change_and_rejects_stale_plan(self):
        binding = {key: self.plan[key] for key in ("id", "revision", "scope_digest")}
        self.assertFault("UNAUTHORIZED", self.ledger.begin_publication, "project", "publication-1", binding, VERIFY, self.key())
        self.assertFault("CONTRACT_STALE", self.ledger.begin_publication, "project", "publication-1",
                         {**binding, "revision": 2}, INTEGRATE, self.key())
        self.ledger.begin_publication("project", "publication-1", binding, INTEGRATE, self.key())
        self.ledger.disable_project("project", OP, self.key())
        changed = {"project_limit": 1, "profile": "cooperative_native", "review_authority": REVIEW.id}
        self.assertFault("INSTANCE_UNCERTAIN", self.ledger.enable_project, "project", str(self.repo), changed, OP, self.key())
        self.ledger.end_publication("project", "publication-1", INTEGRATE, self.key())
        self.ledger.enable_project("project", str(self.repo), changed, OP, self.key())

    def test_publication_begin_is_atomic_across_connections(self):
        binding = {key: self.plan[key] for key in ("id", "revision", "scope_digest")}
        barrier = threading.Barrier(4)
        def begin(index):
            ledger = Ledger(self.path)
            barrier.wait()
            try:
                return ledger.begin_publication("project", "publication-" + str(index), binding, INTEGRATE,
                                                "publication-race-" + str(index))["active"]
            except Fault as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(begin, range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count("SCOPE_CONFLICT"), 3)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)

    def test_policy_update_requires_disabled_quiescent_project(self):
        self.add("a")
        attempt = self.launch("a")
        config = {"project_limit": 1, "profile": "cooperative_native", "review_authority": REVIEW.id}
        self.assertFault("REVISION_CONFLICT", self.ledger.enable_project, "project", str(self.repo), config, OP, self.key())
        self.ledger.disable_project("project", OP, self.key())
        self.assertFault("REVISION_CONFLICT", self.ledger.enable_project, "project", str(self.repo), config, OP, self.key())
        self.finish(attempt)
        updated = self.ledger.enable_project("project", str(self.repo), config, OP, self.key())
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["config"]["project_limit"], 1)

    def test_admission_rechecks_symlinks(self):
        self.add("a", scope=[{"path": "src/a.py", "kind": "file"}])
        outside = self.home / "external"
        outside.mkdir()
        (self.repo / "src").symlink_to(outside, target_is_directory=True)
        self.assertFault("INVALID_SCOPE", self.ledger.reserve, "project", "a", COORD, self.key())
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)

    def test_ledger_reopen_retains_active_occupancy_and_ordered_events(self):
        self.add("a")
        attempt = self.launch("a")
        reopened = Ledger(self.path)
        self.assertEqual(reopened.active_attempts()[0]["attempt_id"], attempt["attempt_id"])
        events = reopened.events()
        sequences = [event["seq"] for event in events]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(reopened.events(after=sequences[-1]), [])
        self.assertEqual(reopened.status()["capacity"]["used"], 1)


if __name__ == "__main__":
    unittest.main()
