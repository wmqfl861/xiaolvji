"""Metrics use real ledger transitions, bounded snapshots and no worker output."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, Principal, canonical_json, digest
from xiaolvji.metrics import runtime_metrics
from xiaolvji.store import Ledger

OP = Principal("operator", "operator")
PLAN = Principal("planner", "planner")
COORD = Principal("coordinator", "coordinator")
EXEC = Principal("executor", "executor")


@contextmanager
def at(seconds):
    stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
    with patch("xiaolvji.store.utc_now", return_value=stamp), patch("xiaolvji.metrics.utc_now", return_value=stamp):
        yield


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root / "state" / "ledger.sqlite3", global_limit=2)
        self.sequence = 0
        with at(0):
            self.project("p")

    def key(self):
        self.sequence += 1
        return "metric-test-" + str(self.sequence)

    def project(self, project):
        root = self.root / project
        root.mkdir()
        self.ledger.enable_project(project, str(root), {"project_limit": 2, "profile": "cooperative_native",
                                                       "review_authority": "reviewer"}, OP, self.key())
        scope = [{"path": "src", "kind": "directory"}]
        self.ledger.register_plan(project, {"id": "plan", "revision": 1, "approved_scope": scope,
                                          "scope_digest": digest(scope), "approval_ref": "test:approved"}, PLAN, self.key())

    def add(self, task, project="p", retries=0):
        plan = self.ledger.project(project)["plan"]
        definition = {"schema_version": 1, "task_id": task,
                      "plan": {key: plan[key] for key in ("id", "revision", "scope_digest")},
                      "role": "worker", "engine": "codex", "objective": "private-objective-marker",
                      "base_commit": "1" * 40, "contract_versions": [], "depends_on": [],
                      "write_scope": [{"path": "src/" + task, "kind": "file"}], "exclusive_resources": [],
                      "acceptance": [{"id": "check", "criterion": "required behavior"}],
                      "expected_artifacts": ["candidate_commit"], "retry": {"max_retries": retries, "backoff_ms": 0},
                      "final_review_required": True}
        self.ledger.register_task(project, definition, PLAN, self.key())

    def reserve(self, task, project="p"):
        return self.ledger.reserve(project, task, COORD, self.key())

    def start(self, attempt):
        self.ledger.claim_launch(attempt["attempt_id"], attempt["generation"], EXEC, self.key())
        return self.ledger.observe(attempt["attempt_id"], attempt["generation"], "started",
                                   {"identity": {"pid": self.sequence, "boot_id": "fixture"}}, EXEC, self.key())

    def finish(self, attempt, code=0, **flags):
        return self.ledger.observe(attempt["attempt_id"], attempt["generation"], "exited",
                                   {"identity": attempt["identity"], "process_tree_terminated": True,
                                    "exit_code": code, **flags}, EXEC, self.key())

    def test_measured_wait_occupancy_refill_and_unknown_charge(self):
        with at(0):
            self.add("a")
        with at(1):
            self.add("b")
        with at(2):
            a = self.reserve("a")
        with at(3):
            a = self.start(a)
        with at(4):
            b = self.reserve("b")
        with at(5):
            b = self.start(b)
        with at(7):
            self.add("c")
        with at(8):
            self.finish(a)
        with at(9):
            c = self.reserve("c")
        with at(10):
            c = self.start(c)
            self.ledger.observe(c["attempt_id"], 1, "started", {"identity": c["identity"]}, EXEC, self.key())
        with at(11):
            self.ledger.observe(c["attempt_id"], 1, "lost", {"reason": "private-reason-marker"}, EXEC, self.key())
        with at(14):
            self.finish(b, timed_out=True)
        with at(15):
            self.finish(a)  # Exact duplicate exit must not add another sample.
        before = self.ledger.events()
        with at(20):
            report = runtime_metrics(self.ledger)
        self.assertEqual(report["event_cursor"], before[-1]["seq"])
        self.assertEqual(self.ledger.events(), before)
        self.assertEqual(report["capacity"]["global"], {"used": 1, "limit": 2, "available": 1, "uncertain": 1})
        self.assertEqual(report["capacity"]["selected_peak_reserved"], 2)
        self.assertEqual(report["seconds"]["registration_to_first_reservation"]["mean"], 2.333333)
        self.assertEqual(report["seconds"]["reservation_to_release"]["samples"], 2)
        self.assertEqual(report["seconds"]["reservation_to_release"]["mean"], 8)
        self.assertEqual(report["seconds"]["first_start_ack_to_terminal_observation"]["mean"], 7)
        self.assertEqual(report["seconds"]["release_to_next_reservation_in_scope"]["mean"], 1)
        self.assertEqual(report["slot_usage"], {"closed_reservation_seconds": 16, "active_reservation_seconds": 11,
                                              "active_intervals_are_incomplete": True, "releases_without_later_reservation": 1})
        self.assertEqual(report["process_outcomes"], {"succeeded": 1, "failed": 0, "timed_out": 1,
                                                     "cancelled": 0, "start_failed": 0})
        for secret in (str(self.root), "private-objective-marker", "private-reason-marker", a["attempt_id"]):
            self.assertNotIn(secret, json.dumps(report))
        self.assertIn("eligible_queue_wait", report["unavailable"])
        self.assertIn("technical_first_pass_rate", report["unavailable"])

    def test_retries_are_attempts_and_projects_do_not_share_task_identity(self):
        with at(0):
            self.add("a", retries=2)
            self.project("q")
            self.add("a", "q")
        for index in range(3):
            with at(index * 3 + 1):
                attempt = self.start(self.reserve("a"))
            with at(index * 3 + 2):
                self.finish(attempt, code=1 if index < 2 else 0)
        with at(10):
            self.start(self.reserve("a", "q"))
        with at(12):
            report = runtime_metrics(self.ledger, "p")
            all_projects = runtime_metrics(self.ledger)
        self.assertEqual(report["population"]["retried_tasks"], 1)
        self.assertEqual(report["population"]["retry_attempts"], 2)
        self.assertEqual(report["population"]["tasks_ever_reserved"], 1)
        self.assertEqual(all_projects["population"]["tasks_ever_reserved"], 2)
        self.assertEqual(report["capacity"]["selected_active"], 0)
        self.assertEqual(report["capacity"]["global"]["used"], 1)
        self.assertEqual(report["process_outcomes"]["failed"], 2)
        self.assertEqual(report["seconds"]["registration_to_first_reservation"]["samples"], 1)

    def test_never_started_and_cancelled_attempts_have_distinct_outcomes(self):
        self.add("a")
        a = self.reserve("a")
        self.ledger.observe(a["attempt_id"], 1, "start_failed", {"no_process_created": True, "reason": "fixture"}, EXEC, self.key())
        self.add("b")
        b = self.start(self.reserve("b"))
        self.finish(b, cancelled=True)
        report = runtime_metrics(self.ledger)
        self.assertEqual(report["process_outcomes"]["start_failed"], 1)
        self.assertEqual(report["process_outcomes"]["cancelled"], 1)
        self.assertEqual(report["seconds"]["first_start_ack_to_terminal_observation"]["samples"], 1)

    def test_backwards_wall_clock_is_reported_and_negative_durations_excluded(self):
        with at(10):
            self.add("a")
        with at(9):
            a = self.reserve("a")
        with at(8):
            a = self.start(a)
        with at(7):
            self.finish(a)
        with at(6):
            report = runtime_metrics(self.ledger)
        self.assertGreaterEqual(report["history"]["timestamp_order_reversals"], 3)
        for name in ("registration_to_first_reservation", "reservation_to_release", "first_start_ack_to_terminal_observation"):
            self.assertEqual(report["seconds"][name]["excluded_clock_reversals"], 1)
            self.assertIsNone(report["seconds"][name]["mean"])
        self.assertEqual(report["slot_usage"]["closed_reservation_seconds"], 0)

    def test_history_gate_counts_remain_historical_after_revocation(self):
        self.add("a")
        binding = {"plan": self.ledger.task("p", "a")["task"]["plan"]}
        for index in range(2):
            self.ledger.record_gate("p", "a", "contract_ready", digest("same-proof"), binding, PLAN, self.key())
        self.ledger.cancel("p", "a", COORD, self.key())
        report = runtime_metrics(self.ledger)
        self.assertEqual(report["historical_quality_records"]["gate_records"]["contract_ready"], 1)
        self.assertTrue(report["historical_quality_records"]["records_may_have_been_revoked"])
        self.assertEqual(report["population"]["stored_task_states"], {"CANCELLED": 1})
        for decision in ("approve", "rework", "reject"):
            self.add(decision)
            attempt = self.start(self.reserve(decision))
            self.finish(attempt)
            plan = attempt["task"]["plan"]
            candidate = {"commit": "2" * 40, "artifact_digest": digest("candidate-" + decision),
                         "plan": plan, "changed_paths": ["src/" + decision]}
            self.ledger.submit_candidate(attempt["attempt_id"], attempt["generation"], candidate, EXEC, self.key())
            self.ledger.record_gate("p", decision, "implementation_verified", candidate["artifact_digest"],
                                    {"plan": plan}, Principal("verifier", "verifier"), self.key())
            integrated = digest("integration-" + decision)
            self.ledger.record_gate("p", decision, "integrated_verified", integrated,
                                    {"plan": plan, "candidate_digest": candidate["artifact_digest"]},
                                    Principal("integrator", "integrator"), self.key())
            self.ledger.review("p", decision, integrated, decision,
                               {"plan": plan, "review_record_id": "review-" + decision},
                               Principal("reviewer", "reviewer"), self.key())
        before_revoke = runtime_metrics(self.ledger)["historical_quality_records"]
        self.assertEqual(before_revoke["review_decisions"], {"approve": 1, "reject": 1, "rework": 1})
        self.assertEqual(before_revoke["gate_records"], {"contract_ready": 1, "implementation_verified": 3,
                                                        "integrated_verified": 3, "final_accepted": 1})
        self.ledger.cancel("p", "approve", COORD, self.key())
        with self.assertRaises(Fault) as caught:
            self.ledger.gate("p", "approve", "final_accepted")
        self.assertEqual(caught.exception.code, "DEPENDENCY_BLOCKED")
        report = runtime_metrics(self.ledger)
        self.assertEqual(report["historical_quality_records"], before_revoke)
        self.assertEqual(report["population"]["stored_task_states"], {"BLOCKED": 2, "CANCELLED": 2})

    def test_one_snapshot_excludes_concurrent_registration(self):
        before = self.ledger.events()[-1]["seq"]
        original = self.ledger._connect
        writer = Ledger(self.ledger.path)
        failures = []
        triggered = []
        def connect():
            conn = original()
            def trace(sql):
                if sql.startswith("SELECT COUNT(*) FROM (SELECT 1 FROM events ") and not triggered:
                    triggered.append(True)
                    try:
                        # The writer uses a separate WAL connection after the
                        # reader established its event-cursor snapshot.
                        with patch.object(self, "ledger", writer):
                            self.add("concurrent")
                    except Exception as exc:
                        failures.append(exc)
            conn.set_trace_callback(trace)
            return conn
        with patch.object(self.ledger, "_connect", side_effect=connect):
            report = runtime_metrics(self.ledger)
        self.assertEqual(failures, [])
        self.assertTrue(triggered)
        self.assertEqual(report["event_cursor"], before)
        self.assertEqual(report["population"]["tasks"], 0)
        self.assertEqual(len(self.ledger.status()["tasks"]), 1)

    def test_bounds_unknown_project_and_invalid_selector_fail_without_partial_report(self):
        self.add("a")
        self.reserve("a")
        for bound in ("MAX_EVENTS", "MAX_TASKS", "MAX_ATTEMPTS"):
            with self.subTest(bound=bound), patch("xiaolvji.metrics." + bound, 0):
                with self.assertRaises(Fault) as caught:
                    runtime_metrics(self.ledger)
                self.assertEqual(caught.exception.code, "METRICS_LIMIT")
        for value, code in (("missing", "NOT_FOUND"), (False, "INVALID_INPUT"), ("", "INVALID_INPUT")):
            with self.subTest(project=value), self.assertRaises(Fault) as caught:
                runtime_metrics(self.ledger, value)
            self.assertEqual(caught.exception.code, code)

    def test_missing_or_corrupt_history_is_not_presented_as_a_complete_measurement(self):
        self.add("a")
        self.add("b")
        first = self.start(self.reserve("a"))
        second = self.start(self.reserve("b"))
        self.finish(first)
        self.finish(second)
        columns = "seq,type,project_id,task_id,attempt_id,occurred_at,payload"
        with closing(self.ledger._connect()) as conn:
            baseline = [tuple(row) for row in conn.execute("SELECT " + columns + " FROM events ORDER BY seq")]
        corruptions = [
            ("missing-" + kind, "DELETE FROM events WHERE type=? AND task_id='a'", (kind,))
            for kind in ("task_registered", "run_start_ack", "resource_released", "run_exit_confirmed")
        ]
        corruptions.extend([
            ("duplicate-terminal",
             "INSERT INTO events(type,project_id,task_id,attempt_id,occurred_at,payload) "
             "SELECT type,project_id,task_id,attempt_id,occurred_at,payload FROM events "
             "WHERE type='run_exit_confirmed' AND task_id='a'", ()),
            ("terminal-bound-to-other-known-attempt",
             "UPDATE events SET attempt_id=? WHERE type='run_exit_confirmed' AND task_id='a'",
             (second["attempt_id"],)),
        ])
        for name, sql, parameters in corruptions:
            with self.subTest(corruption=name):
                with closing(self.ledger._connect()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("DELETE FROM events")
                    conn.executemany("INSERT INTO events(" + columns + ") VALUES(?,?,?,?,?,?,?)", baseline)
                    conn.execute(sql, parameters)
                    conn.commit()
                with self.assertRaises(Fault) as caught:
                    runtime_metrics(self.ledger)
                self.assertEqual(caught.exception.code, "METRICS_INCONSISTENT")

    def test_detail_budget_counts_utf8_bytes_before_parsing_and_accepts_exact_boundary(self):
        self.add("a")
        binding = {"plan": self.ledger.task("p", "a")["task"]["plan"], "notes": "\u7eff" * 100}
        self.ledger.record_gate("p", "a", "contract_ready", digest("proof"), binding, PLAN, self.key())
        before = self.ledger.events()
        detail = canonical_json(next(event["payload"] for event in before if event["type"] == "gate_recorded"))
        detail_bytes = len(detail.encode("utf-8"))
        self.assertGreater(detail_bytes, len(detail))
        with (patch("xiaolvji.metrics.MAX_DETAIL_BYTES", len(detail)),
              patch("xiaolvji.metrics.load_json", side_effect=AssertionError("Budget must precede payload parsing"))):
            with self.assertRaises(Fault) as caught:
                runtime_metrics(self.ledger)
        self.assertEqual(caught.exception.code, "METRICS_LIMIT")
        self.assertEqual(caught.exception.details["population"], "event_detail_bytes")
        self.assertEqual(caught.exception.details["count"], detail_bytes)
        with patch("xiaolvji.metrics.MAX_DETAIL_BYTES", detail_bytes):
            report = runtime_metrics(self.ledger)
        self.assertEqual(report["history"]["detail_bytes"], detail_bytes)
        self.assertEqual(report["historical_quality_records"]["gate_records"]["contract_ready"], 1)
        self.assertEqual(self.ledger.events(), before)

    def test_empty_domain_has_no_zero_duration_success_samples(self):
        empty = Ledger(self.root / "empty" / "ledger.sqlite3")
        report = runtime_metrics(empty)
        self.assertEqual(report["event_cursor"], 0)
        self.assertTrue(report["history"]["complete"])
        self.assertEqual(report["capacity"]["selected_active"], 0)
        for values in report["seconds"].values():
            self.assertEqual(values["samples"], 0)
            self.assertIsNone(values["mean"])


if __name__ == "__main__":
    unittest.main()
