"""Fair ordering and bounded diagnostics over real ledger transactions."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from xiaolvji import scheduling
from xiaolvji.store import Ledger
from tests import test_store as store_tests

COORD, EXEC, PLAN = store_tests.COORD, store_tests.EXEC, store_tests.PLAN

START = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def iso(instant):
    return datetime.fromtimestamp(instant, timezone.utc).isoformat().replace("+00:00", "Z")


class RankingTests(unittest.TestCase):
    def test_quality_is_a_bounded_preference(self):
        worker = scheduling.rank({"role": "worker"}, iso(START), START)
        integrator = scheduling.rank({"role": "integrator"}, iso(START), START)
        verifier = scheduling.rank({"role": "verifier"}, iso(START), START)
        self.assertEqual([50, 60, 70], [entry["effective_rank"] for entry in (worker, integrator, verifier)])
        self.assertEqual(50, worker["priority"])
        self.assertEqual(0, worker["aging_credit"])

    def test_low_priority_worker_reaches_common_ceiling(self):
        low = {"role": "worker", "priority": 0}
        self.assertEqual(119, scheduling.rank(low, iso(START), START + 3599)["effective_rank"])
        self.assertEqual(120, scheduling.rank(low, iso(START), START + 3600)["effective_rank"])
        self.assertEqual(120, scheduling.rank(low, iso(START), START + 999999)["effective_rank"])
        new = scheduling.rank({"role": "verifier", "priority": 100}, iso(START + 3600), START + 3600)
        self.assertEqual(120, new["effective_rank"])
        self.assertLess(scheduling.order_key(120, 3, "a", "old"), scheduling.order_key(120, 8, "a", "new"))

    def test_clock_rollback_cannot_create_negative_age_or_rank(self):
        result = scheduling.rank({"role": "worker", "priority": 0}, iso(START), START - 100)
        self.assertEqual(0, result["waiting_age_seconds"])
        self.assertEqual(0, result["effective_rank"])

    def test_policy_is_detached_and_names_aging_limits(self):
        first = scheduling.policy()
        first["role_bonus"]["verifier"] = 999
        self.assertEqual(20, scheduling.policy()["role_bonus"]["verifier"])
        self.assertEqual(3600, first["maximum_seconds_to_ceiling"])
        self.assertFalse(first["start_deadline_guaranteed"])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store_tests.LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)
        self.ledger = self.fixture.ledger

    def register(self, task_id, *, at=START, priority=50, role="worker", resources=None, **kwargs):
        definition = self.fixture.definition(task_id, **kwargs)
        definition.update(priority=priority, role=role)
        if resources is not None:
            definition["exclusive_resources"] = resources
        with patch("xiaolvji.store.utc_now", return_value=iso(at)):
            self.ledger.register_task(kwargs.get("project_id", "project"), definition, PLAN, self.fixture.key())
        return definition

    def queue(self, *, now=START, project_id=None, limit=100):
        with patch("xiaolvji.store.time.time", return_value=now):
            return self.ledger.queue(project_id, limit)

    def entry(self, task_id, **kwargs):
        return next(entry for entry in self.queue(**kwargs)["entries"] if entry["task_id"] == task_id)

    def test_aging_survives_restart_and_matches_ready_dispatch_order(self):
        self.register("old", priority=0)
        self.register("new", at=START + 3600, priority=100, role="verifier")
        reopened = Ledger(self.fixture.path)
        with patch("xiaolvji.store.time.time", return_value=START + 3600):
            ready = reopened.ready_tasks()
            queue = reopened.queue()
        self.assertEqual(["old", "new"], [entry["task_id"] for entry in ready])
        self.assertEqual([entry["task_id"] for entry in ready], [entry["task_id"] for entry in queue["entries"]])
        self.assertEqual([120, 120], [entry["effective_rank"] for entry in queue["entries"]])
        self.assertEqual([3600, 0], [entry["waiting_age_seconds"] for entry in queue["entries"]])

    def test_sustained_new_verifiers_do_not_starve_aged_worker(self):
        self.register("old", priority=0)
        for index in range(3):
            instant = START + 30 * index
            self.register("verify-" + str(index), at=instant, priority=100, role="verifier")
            with patch("xiaolvji.store.time.time", return_value=instant):
                self.assertEqual("verify-" + str(index), self.ledger.ready_tasks()[0]["task_id"])
                attempt = self.fixture.launch("verify-" + str(index))
                self.fixture.finish(attempt)
        self.register("newest", at=START + 3600, priority=100, role="verifier")
        with patch("xiaolvji.store.time.time", return_value=START + 3600):
            self.assertEqual("old", self.ledger.ready_tasks()[0]["task_id"])

    def test_aging_does_not_bypass_missing_quality_evidence(self):
        self.register("producer")
        self.register("blocked", priority=100, role="verifier",
                      depends=[{"task_id": "producer", "gate": "implementation_verified"}])
        self.register("ready", at=START + 7200, priority=0)
        result = self.queue(now=START + 7200)
        blocked = next(entry for entry in result["entries"] if entry["task_id"] == "blocked")
        self.assertEqual(120, blocked["effective_rank"])
        self.assertFalse(blocked["eligible"])
        self.assertFalse(blocked["dispatchable"])
        self.assertEqual("DEPENDENCY_BLOCKED", blocked["blockers"][0]["code"])
        self.assertEqual("blocked", result["entries"][-1]["task_id"])
        self.fixture.assertFault("DEPENDENCY_BLOCKED", self.ledger.reserve,
                                 "project", "blocked", COORD, self.fixture.key())

    def test_global_capacity_counts_other_projects(self):
        self.fixture.project("other", self.fixture.home / "other")
        self.ledger.register_plan("other", self.fixture.make_plan(), PLAN, self.fixture.key())
        self.register("a", project_id="other")
        self.register("b", project_id="other")
        self.register("waiting")
        self.ledger.reserve("other", "a", COORD, self.fixture.key())
        self.ledger.reserve("other", "b", COORD, self.fixture.key())
        result = self.queue(project_id="project")
        self.assertEqual({"used": 2, "limit": 2, "available": 0, "uncertain": 0}, result["capacity"])
        entry = result["entries"][0]
        self.assertTrue(entry["eligible"])
        self.assertFalse(entry["dispatchable"])
        self.assertEqual([{"code": "CAPACITY_FULL", "kind": "global_capacity"}], entry["blockers"])
        self.assertEqual({"used": 0, "limit": 2, "available": 0}, entry["project_capacity"])

    def test_project_capacity_has_its_own_reason(self):
        self.fixture.project("small", self.fixture.home / "small", limit=1)
        self.ledger.register_plan("small", self.fixture.make_plan(), PLAN, self.fixture.key())
        self.register("holder", project_id="small")
        self.register("waiter", project_id="small")
        self.ledger.reserve("small", "holder", COORD, self.fixture.key())
        entry = self.entry("waiter", project_id="small")
        self.assertEqual([{"code": "CAPACITY_FULL", "kind": "project_capacity"}], entry["blockers"])
        self.assertFalse(entry["dispatchable"])
        self.assertEqual(1, self.queue()["capacity"]["available"])

    def test_conflict_summaries_identify_owners_without_scopes_or_resources(self):
        self.register("holder", scope=[{"path": "src/shared", "kind": "directory"}],
                      resources=["sensitive-resource"])
        self.register("waiter", scope=[{"path": "src/shared/item.py", "kind": "file"}],
                      resources=["sensitive-resource"])
        attempt = self.ledger.reserve("project", "holder", COORD, self.fixture.key())
        entry = self.entry("waiter")
        self.assertEqual({"exclusive_resource", "write_scope"}, {blocker["kind"] for blocker in entry["blockers"]})
        self.assertEqual(2, entry["blocker_count"])
        for blocker in entry["blockers"]:
            self.assertEqual({"attempt_id": attempt["attempt_id"], "project_id": "project", "task_id": "holder"},
                             blocker["owner"])
        self.assertNotIn("sensitive-resource", json.dumps(entry))
        self.assertNotIn("src/shared", json.dumps(entry))
        self.fixture.assertFault("SCOPE_CONFLICT", self.ledger.reserve,
                                 "project", "waiter", COORD, self.fixture.key())

    def test_multiple_leases_from_one_owner_are_coalesced(self):
        self.register("holder", scope=[{"path": "src/shared/a.py", "kind": "file"},
                                       {"path": "src/shared/b.py", "kind": "file"}],
                      resources=["one", "two"])
        self.register("waiter", scope=[{"path": "src/shared", "kind": "directory"}], resources=["one", "two"])
        self.ledger.reserve("project", "holder", COORD, self.fixture.key())
        self.assertEqual(2, self.entry("waiter")["blocker_count"])

    def test_conflict_summaries_are_bounded_with_honest_total(self):
        self.ledger = self.fixture.ledger = Ledger(self.fixture.home / "large-state" / "ledger.sqlite3", global_limit=32)
        self.fixture.project("project", self.fixture.repo, limit=32)
        self.ledger.register_plan("project", self.fixture.make_plan(), PLAN, self.fixture.key())
        for index in range(18):
            name = "holder-" + str(index)
            self.register(name, scope=[{"path": f"src/shared/{index}.py", "kind": "file"}])
            self.ledger.reserve("project", name, COORD, self.fixture.key())
        self.register("waiter", scope=[{"path": "src/shared", "kind": "directory"}])
        entry = self.entry("waiter")
        self.assertEqual(18, entry["blocker_count"])
        self.assertEqual(16, len(entry["blockers"]))
        self.assertTrue(entry["blockers_truncated"])
        self.assertFalse(entry["dispatchable"])

    def test_filesystem_scope_is_rechecked_in_queue_and_reservation(self):
        self.register("changed")
        outside = self.fixture.home / "outside"
        outside.mkdir()
        (self.fixture.repo / "src").symlink_to(outside, target_is_directory=True)
        entry = self.entry("changed")
        self.assertTrue(entry["eligible"])
        self.assertEqual([{"code": "INVALID_SCOPE", "kind": "scope_invalid"}], entry["blockers"])
        self.assertFalse(entry["dispatchable"])
        self.fixture.assertFault("INVALID_SCOPE", self.ledger.reserve,
                                 "project", "changed", COORD, self.fixture.key())
        self.assertEqual(0, self.ledger.status()["capacity"]["used"])

    def test_retry_backoff_uses_the_same_sampled_instant_as_age(self):
        definition = self.fixture.definition("retry", retries=1)
        definition["retry"]["backoff_ms"] = 60_000
        with patch("xiaolvji.store.utc_now", return_value=iso(START)):
            self.ledger.register_task("project", definition, PLAN, self.fixture.key())
        with patch("xiaolvji.store.time.time", return_value=START):
            attempt = self.fixture.launch("retry")
            self.fixture.finish(attempt, code=1)
        with patch("xiaolvji.store.time.time", side_effect=[START + 59]) as clock:
            entry = self.ledger.queue()["entries"][0]
        self.assertEqual(1, clock.call_count)
        self.assertFalse(entry["eligible"])
        self.assertEqual(59, entry["waiting_age_seconds"])
        self.assertEqual(1, entry["blockers"][0]["retry_after_seconds"])
        self.assertEqual("RETRY_BACKOFF", entry["blockers"][0]["code"])
        self.assertTrue(self.entry("retry", now=START + 60)["dispatchable"])

    def test_exhausted_retry_is_diagnosed_without_reopening_budget(self):
        self.register("exhausted")
        attempt = self.fixture.launch("exhausted")
        self.fixture.finish(attempt, code=1)
        entry = self.entry("exhausted")
        self.assertFalse(entry["eligible"])
        self.assertFalse(entry["dispatchable"])
        self.assertEqual("RETRY_EXHAUSTED", entry["blockers"][0]["code"])
        self.fixture.assertFault("DEPENDENCY_BLOCKED", self.ledger.reserve,
                                 "project", "exhausted", COORD, self.fixture.key())

    def test_unknown_process_still_charges_capacity(self):
        self.register("lost")
        self.register("waiting")
        attempt = self.fixture.launch("lost")
        self.ledger.observe(attempt["attempt_id"], attempt["generation"], "lost",
                            {"reason": "transport disconnected"}, EXEC, self.fixture.key())
        result = self.queue()
        self.assertEqual(1, result["capacity"]["used"])
        self.assertEqual(1, result["capacity"]["uncertain"])
        self.assertEqual(1, result["capacity"]["available"])

    def test_preview_is_read_only_and_bounded_without_raw_definitions(self):
        for name in ("a", "b", "c"):
            self.register(name)
        original_connect = self.ledger._connect
        statements = []
        def traced_connect():
            conn = original_connect()
            conn.set_trace_callback(statements.append)
            return conn
        before = self.ledger.status()["event_cursor"]
        with patch.object(self.ledger, "_connect", side_effect=traced_connect):
            result = self.queue(limit=2)
        self.assertTrue(result["advisory"])
        self.assertEqual(3, result["total"])
        self.assertTrue(result["truncated"])
        self.assertEqual(2, len(result["entries"]))
        self.assertEqual(before, result["event_cursor"])
        self.assertEqual(before, self.ledger.status()["event_cursor"])
        self.assertEqual(1, sum(statement == "BEGIN" for statement in statements))
        self.assertFalse(any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                             for statement in statements))
        self.assertEqual([], self.ledger.launches())
        encoded = json.dumps(result)
        for field in ("objective", "definition", "argv", "write_scope", "root", "acceptance", "candidate"):
            self.assertNotIn('"' + field + '"', encoded)

    def test_snapshot_stays_consistent_when_another_connection_reserves(self):
        self.register("holder")
        self.register("waiter")
        other = Ledger(self.fixture.path)
        original_connect = self.ledger._connect
        reserved = []
        before = self.ledger.status()["event_cursor"]
        def traced_connect():
            conn = original_connect()
            def trace(statement):
                if "FROM tasks t LEFT JOIN events e" in statement and not reserved:
                    reserved.append(other.reserve("project", "holder", COORD, self.fixture.key()))
            conn.set_trace_callback(trace)
            return conn
        with patch.object(self.ledger, "_connect", side_effect=traced_connect):
            result = self.queue()
        self.assertEqual(1, len(reserved))
        self.assertEqual(before, result["event_cursor"])
        self.assertEqual(0, result["capacity"]["used"])
        self.assertTrue(all(entry["dispatchable"] for entry in result["entries"]))
        self.assertEqual(1, self.ledger.status()["capacity"]["used"])
        self.assertGreater(self.ledger.status()["event_cursor"], result["event_cursor"])

    def test_reservation_rechecks_after_a_dispatchable_preview(self):
        self.register("holder", resources=["shared"])
        self.register("waiter", resources=["shared"])
        self.assertTrue(self.entry("waiter")["dispatchable"])
        self.ledger.reserve("project", "holder", COORD, self.fixture.key())
        self.fixture.assertFault("SCOPE_CONFLICT", self.ledger.reserve,
                                 "project", "waiter", COORD, self.fixture.key())

    def test_invalid_limits_and_unknown_project_fail_explicitly(self):
        for value in (True, False, 0, -1, 201, 1.0, "2", None):
            with self.subTest(limit=value):
                self.fixture.assertFault("INVALID_REQUEST", self.ledger.queue, None, value)
        self.fixture.assertFault("NOT_FOUND", self.ledger.queue, "missing")
        self.fixture.assertFault("INVALID_REQUEST", self.ledger.queue, "")
        self.assertEqual(0, self.queue(limit=200)["total"])

    def test_population_budget_fails_before_ranking_even_for_one_result(self):
        for name in ("a", "b", "c"):
            self.register(name)
        with patch("xiaolvji.store._QUEUE_MAX_TASKS", 2), patch("xiaolvji.scheduling.rank") as rank:
            error = self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue, None, 1)
        self.assertEqual({"population": "tasks", "limit": 2, "at_least": 3}, error.details)
        self.assertIn("select one project", error.message)
        rank.assert_not_called()
        with patch("xiaolvji.store._QUEUE_MAX_TASKS", 2):
            self.assertEqual(3, len(self.ledger.ready_tasks()))

    def test_project_filter_reduces_complete_task_population(self):
        self.fixture.project("other", self.fixture.home / "other")
        self.ledger.register_plan("other", self.fixture.make_plan(), PLAN, self.fixture.key())
        self.register("a")
        self.register("b")
        self.register("c", project_id="other")
        with patch("xiaolvji.store._QUEUE_MAX_TASKS", 2):
            self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue)
            self.assertEqual(2, self.queue(project_id="project")["total"])

    def test_attempt_and_lease_budgets_remain_domain_wide(self):
        self.register("a", resources=["shared"])
        self.register("b")
        self.ledger.reserve("project", "a", COORD, self.fixture.key())
        self.ledger.reserve("project", "b", COORD, self.fixture.key())
        with patch("xiaolvji.store._QUEUE_MAX_ACTIVE_ATTEMPTS", 1):
            error = self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue, "project")
        self.assertEqual({"population": "active_attempts", "limit": 1, "at_least": 2}, error.details)
        with patch("xiaolvji.store._QUEUE_MAX_LEASES", 1):
            error = self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue, "project")
        self.assertEqual({"population": "leases", "limit": 1, "at_least": 2}, error.details)
        self.assertEqual(2, self.ledger.status()["capacity"]["used"])

    def test_empty_projects_cannot_create_unbounded_diagnostic_materialization(self):
        self.fixture.project("other", self.fixture.home / "other")
        with patch("xiaolvji.store._QUEUE_MAX_PROJECTS", 1):
            error = self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue)
            self.assertEqual(0, self.queue(project_id="project")["total"])
        self.assertEqual({"population": "projects", "limit": 1, "at_least": 2}, error.details)

    def test_population_counters_share_the_same_read_snapshot(self):
        self.register("a")
        self.register("b")
        definition = self.fixture.definition("concurrent")
        other = Ledger(self.fixture.path)
        original_connect = self.ledger._connect
        added = []
        before = self.ledger.status()["event_cursor"]
        def traced_connect():
            conn = original_connect()
            def trace(statement):
                if "FROM (SELECT 1 FROM tasks" in statement and not added:
                    added.append(other.register_task("project", definition, PLAN, self.fixture.key()))
            conn.set_trace_callback(trace)
            return conn
        with patch.object(self.ledger, "_connect", side_effect=traced_connect), \
                patch("xiaolvji.store._QUEUE_MAX_TASKS", 2):
            result = self.queue()
        self.assertEqual(1, len(added))
        self.assertEqual(before, result["event_cursor"])
        self.assertEqual(2, result["total"])
        self.assertEqual(["a", "b"], [entry["task_id"] for entry in result["entries"]])
        self.assertEqual(3, len(self.ledger.status()["tasks"]))
        with patch("xiaolvji.store._QUEUE_MAX_TASKS", 2):
            self.fixture.assertFault("QUEUE_LIMIT", self.ledger.queue)

    def test_missing_registration_event_fails_instead_of_hiding_task(self):
        self.register("missing")
        with self.ledger._connect() as conn:
            conn.execute("DELETE FROM events WHERE seq=(SELECT created_seq FROM tasks WHERE task_id='missing')")
        self.fixture.assertFault("QUEUE_INCONSISTENT", self.ledger.queue)
        self.fixture.assertFault("QUEUE_INCONSISTENT", self.ledger.ready_tasks)

    def test_misbound_registration_history_fails_with_bounded_errors(self):
        self.register("bad-history")
        changes = [
            ("type", "run_reserved"), ("project_id", "other"), ("task_id", "other"),
            ("occurred_at", "private-invalid-timestamp"), ("occurred_at", "2026-01-01T00:00:00"),
        ]
        for column, value in changes:
            with self.subTest(column=column, value=value):
                with self.ledger._connect() as conn:
                    conn.execute("UPDATE events SET type='task_registered',project_id='project',task_id='bad-history',"
                                 "occurred_at=? WHERE seq=(SELECT created_seq FROM tasks WHERE task_id='bad-history')",
                                 (iso(START),))
                    conn.execute("UPDATE events SET " + column + "=? "
                                 "WHERE seq=(SELECT created_seq FROM tasks WHERE task_id='bad-history')", (value,))
                error = self.fixture.assertFault("QUEUE_INCONSISTENT", self.ledger.queue)
                self.assertNotIn("private-invalid-timestamp", json.dumps(error.as_dict()))
                self.assertLess(len(json.dumps(error.as_dict())), 200)


if __name__ == "__main__":
    unittest.main()
