"""Deterministic controller race/recovery regressions with a real SQLite ledger.

The controlled gateway below is a test double, not proof of host/OS behavior.
The controller lock test uses a separate OS process and a real flock.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import unittest

from tests import test_store as fixtures
from xiaolvji.contracts import Fault
from xiaolvji.dispatcher import Dispatcher
from xiaolvji.store import Ledger


class ControlledGateway:
    def __init__(self, root, task_ids=()):
        self.state_dir = Path(root)
        self.profile = "cooperative_native"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.started = {name: asyncio.Event() for name in task_ids}
        self.release = {name: asyncio.Event() for name in task_ids}
        self.done = {name: asyncio.Event() for name in task_ids}
        self.identities = {}
        self.results = {}
        self.run_order = []
        self.cancel_calls = []
        self.by_attempt = {}
        self.cancelled = set()

    def probe(self):
        return {"supported": True}

    async def run(self, attempt, argv, cwd, env=None, stdin_text=None, timeout=3600, on_started=None):
        name, identifier = attempt["task_id"], attempt["attempt_id"]
        self.started.setdefault(name, asyncio.Event())
        self.release.setdefault(name, asyncio.Event())
        self.done.setdefault(name, asyncio.Event())
        self.by_attempt[identifier] = name
        identity = {"attempt_id": identifier, "generation": attempt["generation"],
                    "pid": 1000 + len(self.run_order), "start_ticks": identifier, "boot_id": "test-gateway"}
        self.identities[identifier] = identity
        self.run_order.append(name)
        if on_started:
            await on_started(identity)
        self.started[name].set()
        await self.release[name].wait()
        result = {"identity": identity, "exit_code": -15 if identifier in self.cancelled else 0,
                  "process_tree_terminated": True, "cancelled": identifier in self.cancelled,
                  "timed_out": False, "stdout_path": "test-stdout", "stderr_path": "test-stderr"}
        self.results[identifier] = result
        self.done[name].set()
        return result

    def reconcile(self, identity):
        identifier = identity.get("attempt_id")
        if identifier in self.results:
            return self.results[identifier]
        for result in self.results.values():
            if result["identity"] == identity:
                return result
        return {"identity": identity, "status": "live", "process_tree_terminated": False}

    async def cancel(self, identifier, grace=2.0):
        self.cancel_calls.append(identifier)
        if identifier not in self.by_attempt:
            return {"status": "uncertain", "process_tree_terminated": False}
        self.cancelled.add(identifier)
        name = self.by_attempt[identifier]
        self.release[name].set()
        await self.done[name].wait()
        return self.results[identifier]


class MinimalPipeline:
    def __init__(self, root):
        self.root = str(root)
        self.prepared = []
        self.finished = []
        self.failed_jobs = []
        self.recovered = []
        self.recovery_failure = None
        self.prepare_entered = asyncio.Event()
        self.allow_prepare = None

    async def prepare(self, attempt):
        self.prepared.append(attempt["attempt_id"])
        self.prepare_entered.set()
        if self.allow_prepare:
            await self.allow_prepare.wait()
        return {"argv": ["fixture-worker"], "cwd": self.root}

    async def finish(self, attempt, job, result):
        self.finished.append(attempt["task_id"])

    async def failed(self, attempt, job, result):
        self.failed_jobs.append(attempt["task_id"])

    async def discover(self, project_id=None):
        return False

    async def recover_attempt(self, attempt, result):
        if self.recovery_failure:
            raise self.recovery_failure
        self.recovered.append(attempt["task_id"])

    async def recover_completed(self, project_id=None):
        pass


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)
        self.ledger = self.fixture.ledger
        self.pipeline = MinimalPipeline(self.fixture.repo)
        self.executor = ControlledGateway(self.fixture.path.parent / "executor")
        self.controller = Dispatcher(self.ledger, self.executor, self.pipeline)

    async def wait(self, event):
        await asyncio.wait_for(event.wait(), timeout=5)

    def releases(self):
        return [event for event in self.ledger.events() if event["type"] == "resource_released"]

    def accept_candidate(self, task_id, candidate):
        self.fixture.gate(task_id, "implementation_verified", candidate["artifact_digest"])
        integration = fixtures.digest("integration-" + task_id)
        self.fixture.gate(task_id, "integrated_verified", integration,
                          candidate_digest=candidate["artifact_digest"])
        self.fixture.gate(task_id, "final_accepted", integration,
                          review_record_id="review-" + task_id)
        return self.ledger.gate("project", task_id, "final_accepted", integration)

    async def test_duplicate_claim_loser_cannot_change_winner_state(self):
        self.fixture.add("a")
        attempt = self.ledger.reserve("project", "a", fixtures.COORD, "reserve-race")
        self.pipeline.allow_prepare = asyncio.Event()
        winner = asyncio.create_task(self.controller._run_one(attempt))
        try:
            await self.wait(self.pipeline.prepare_entered)
            loser = Dispatcher(self.ledger, self.executor, self.pipeline)
            await loser._run_one(attempt)
            current = self.ledger.attempt(attempt["attempt_id"])
            self.assertEqual(current["state"], "STARTING")
            self.assertEqual(current["launch_state"], "CLAIMED")
            self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
            self.assertEqual(self.releases(), [])
            self.assertEqual(len(self.pipeline.prepared), 1)
            self.assertEqual(loser.errors[0]["error"]["code"], "INSTANCE_UNCERTAIN")
            self.pipeline.allow_prepare.set()
            # Gateway creates the event at run entry, after prepare has resumed.
            while "a" not in self.executor.started:
                await asyncio.sleep(0)
            await self.wait(self.executor.started["a"])
            self.executor.release["a"].set()
            await asyncio.wait_for(winner, 5)
            self.assertEqual(self.executor.run_order, ["a"])
            self.assertEqual(self.ledger.status()["capacity"]["used"], 0)
            self.assertEqual(self.controller.errors, [])
        finally:
            self.pipeline.allow_prepare.set()
            for release in self.executor.release.values():
                release.set()
            await asyncio.gather(winner, return_exceptions=True)

    @unittest.skipUnless(os.name == "posix", "Runtime controller lock requires POSIX")
    async def test_os_controller_lock_rejects_second_process_and_recovers_after_exit(self):
        lock_path = self.executor.state_dir.parent / "dispatcher.lock"
        code = ("import fcntl,sys; f=open(sys.argv[1],'a+'); "
                "fcntl.flock(f.fileno(),fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()")
        owner = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(lock_path),
                                                     stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE)
        try:
            self.assertEqual(await asyncio.wait_for(owner.stdout.readline(), 5), b"locked\n")
            with self.assertRaises(Fault) as caught:
                await self.controller.run(timeout=1)
            self.assertEqual(caught.exception.code, "CONTROLLER_RUNNING")
        finally:
            owner.stdin.close()
            await asyncio.wait_for(owner.wait(), 5)
        result = await self.controller.run(timeout=1)
        self.assertEqual(result["status"]["capacity"]["used"], 0)
        self.assertEqual(result["stop_reason"], "no_eligible_work")

    async def test_pending_cancellation_releases_only_atomically_unclaimed_launch(self):
        self.fixture.add("a")
        attempt = self.ledger.reserve("project", "a", fixtures.COORD, "reserve-pending")
        self.assertEqual(attempt["launch_state"], "PENDING")
        result = await self.controller.cancel("project", "a")
        self.assertEqual(result[0]["status"], "cancelled_before_launch")
        self.assertEqual(self.ledger.attempt(attempt["attempt_id"])["state"], "RELEASED")
        self.assertEqual(self.ledger.task("project", "a")["state"], "CANCELLED")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)
        self.assertEqual(self.executor.cancel_calls, [])
        self.assertEqual(len(self.releases()), 1)
        self.assertEqual(await self.controller.cancel("project", "a"), [])
        self.assertEqual(len(self.releases()), 1)

    async def test_claimed_unknown_cancellation_keeps_slot(self):
        self.fixture.add("a")
        attempt = self.ledger.reserve("project", "a", fixtures.COORD, "reserve-claimed")
        self.ledger.claim_launch(attempt["attempt_id"], 1, fixtures.EXEC, "claim-before-crash")
        result = await self.controller.cancel("project", "a")
        self.assertEqual(result[0]["status"], "uncertain")
        current = self.ledger.attempt(attempt["attempt_id"])
        self.assertEqual(current["state"], "STOPPING")
        self.assertEqual(current["launch_state"], "CLAIMED")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.assertEqual(self.releases(), [])

    async def test_recovered_postprocessing_failure_blocks_delivery_after_confirmed_exit(self):
        self.fixture.add("a")
        attempt = self.fixture.launch("a")
        self.executor.results[attempt["attempt_id"]] = {"identity": attempt["identity"], "exit_code": 0,
                                                        "process_tree_terminated": True}
        self.pipeline.recovery_failure = Fault("SCOPE_CONFLICT", "Recovered candidate changed an unleased path")
        await self.controller.reconcile()
        current = self.ledger.attempt(attempt["attempt_id"])
        self.assertEqual(current["state"], "RELEASED")
        self.assertEqual(self.ledger.status()["capacity"]["used"], 0)
        self.assertEqual(self.ledger.task("project", "a")["state"], "BLOCKED")
        self.assertEqual(len(self.releases()), 1)
        self.assertFalse(any(event["type"] == "run_lost" for event in self.ledger.events()))
        self.assertEqual(self.controller.errors[0]["error"]["code"], "SCOPE_CONFLICT")

    async def test_duplicate_exit_does_not_free_another_running_slot(self):
        self.fixture.add("a")
        self.fixture.add("b")
        first = self.fixture.launch("a")
        result = {"identity": first["identity"], "exit_code": 0, "process_tree_terminated": True}
        self.controller._observe_exit(first, result)
        second = self.fixture.launch("b")
        self.controller._observe_exit(first, result)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
        self.assertEqual(self.ledger.active_attempts()[0]["attempt_id"], second["attempt_id"])
        self.assertEqual(len(self.releases()), 1)

    async def test_rolling_refill_starts_next_task_while_long_sibling_is_running(self):
        for task in ("slow", "fast", "next"):
            self.fixture.add(task)
            self.executor.started[task] = asyncio.Event()
            self.executor.release[task] = asyncio.Event()
            self.executor.done[task] = asyncio.Event()
        running = asyncio.create_task(self.controller.run(timeout=10))
        try:
            await self.wait(self.executor.started["slow"])
            await self.wait(self.executor.started["fast"])
            self.assertFalse(self.executor.started["next"].is_set())
            self.assertEqual(self.ledger.status()["capacity"]["used"], 2)
            self.executor.release["fast"].set()
            await self.wait(self.executor.started["next"])
            self.assertFalse(self.executor.done["slow"].is_set())
            self.assertEqual(self.ledger.status()["capacity"]["used"], 2)
            self.assertEqual(self.executor.run_order, ["slow", "fast", "next"])
            self.executor.release["slow"].set()
            self.executor.release["next"].set()
            result = await asyncio.wait_for(running, 5)
            self.assertEqual(result["status"]["capacity"]["used"], 0)
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(self.releases()), 3)
        finally:
            for release in self.executor.release.values():
                release.set()
            await asyncio.gather(running, return_exceptions=True)

    async def test_watch_consumes_new_work_after_each_idle_period(self):
        idle = asyncio.Event()
        self.controller.event_callback = lambda event: idle.set() if event["type"] == "idle_waiting" else None
        running = asyncio.create_task(self.controller.run(timeout=10, watch=True))
        # Registration uses an independent SQLite connection, as an MCP server
        # or a second CLI process would. No in-process wake notification is used.
        writer = Ledger(self.fixture.path)
        try:
            await self.wait(idle)
            for name in ("first", "later"):
                self.assertFalse(running.done())
                idle.clear()
                self.executor.started[name] = asyncio.Event()
                self.executor.release[name] = asyncio.Event()
                self.executor.done[name] = asyncio.Event()
                writer.register_task("project", self.fixture.definition(name), fixtures.PLAN, "external-" + name)
                await self.wait(self.executor.started[name])
                self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
                self.executor.release[name].set()
                await self.wait(idle)
                self.assertEqual(self.ledger.status()["capacity"]["used"], 0)
            self.assertEqual(self.executor.run_order, ["first", "later"])
            self.assertEqual(self.controller.errors, [])
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_watch_keeps_uncertain_slot_charged_without_repeating_lost_events(self):
        self.fixture.add("uncertain")
        attempt = self.ledger.reserve("project", "uncertain", fixtures.COORD, "watch-unknown")
        self.ledger.claim_launch(attempt["attempt_id"], 1, fixtures.EXEC, "watch-claimed")
        idle = asyncio.Event()
        self.controller.event_callback = lambda event: idle.set() if event["type"] == "idle_waiting" else None
        running = asyncio.create_task(self.controller.run(timeout=10, watch=True))
        try:
            await self.wait(idle)
            await self.controller.reconcile()
            await self.controller.reconcile()
            self.assertEqual(len([e for e in self.ledger.events() if e["type"] == "run_lost"]), 1)
            self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
            self.executor.started["next"] = asyncio.Event()
            self.executor.release["next"] = asyncio.Event()
            self.executor.done["next"] = asyncio.Event()
            self.fixture.add("next")
            await self.wait(self.executor.started["next"])
            self.assertEqual(self.ledger.status()["capacity"]["used"], 2)
            self.assertEqual(self.ledger.attempt(attempt["attempt_id"])["state"], "LOST")
            self.executor.release["next"].set()
            await self.wait(self.executor.done["next"])
            self.assertFalse(running.done())
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(self.ledger.status()["capacity"]["used"], 1)

    async def test_deadline_expiring_during_recovery_does_not_launch_more_work(self):
        self.fixture.add("late")
        queued = self.ledger.task("project", "late")
        original = self.pipeline.recover_completed
        async def slow_recovery(project_id=None):
            await asyncio.sleep(0.05)
            return await original(project_id)
        self.pipeline.recover_completed = slow_recovery
        result = await self.controller.run(timeout=0.01, watch=True)
        self.assertEqual(self.executor.run_order, [])
        self.assertEqual(result["stop_reason"], "timeout_or_cancel")
        self.assertTrue(result["watch"])
        self.assertEqual(result["status"]["capacity"]["used"], 0)
        self.assertEqual(self.ledger.task("project", "late"), queued)
        self.assertTrue(self.ledger.project("project")["enabled"])

    async def test_cancellation_during_initial_recovery_stops_live_attempt_and_keeps_unknown_charged(self):
        for name in ("live", "unknown", "queued"):
            self.fixture.add(name)
        live = self.ledger.reserve("project", "live", fixtures.COORD, self.fixture.key())
        self.ledger.claim_launch(live["attempt_id"], 1, fixtures.EXEC, self.fixture.key())
        unknown = self.ledger.reserve("project", "unknown", fixtures.COORD, self.fixture.key())
        self.ledger.claim_launch(unknown["attempt_id"], 1, fixtures.EXEC, self.fixture.key())
        self.executor.started["live"] = asyncio.Event()
        recovery_entered = asyncio.Event()
        recovery_block = asyncio.Event()
        closed = []

        async def on_started(identity):
            self.ledger.observe(live["attempt_id"], 1, "started", {"identity": identity},
                                fixtures.EXEC, self.fixture.key())

        async def blocked_recovery(project_id=None):
            recovery_entered.set()
            await recovery_block.wait()

        async def close():
            closed.append(True)

        self.pipeline.recover_completed = blocked_recovery
        self.pipeline.close = close
        # A supervisor already running when this dispatcher takes ownership.
        worker = asyncio.create_task(self.executor.run(live, ["fixture-worker"], self.fixture.repo,
                                                       on_started=on_started))
        running = None
        try:
            await self.wait(self.executor.started["live"])
            running = asyncio.create_task(self.controller.run("project", timeout=10, watch=True))
            await self.wait(recovery_entered)
            self.assertEqual(self.ledger.attempt(live["attempt_id"])["state"], "RUNNING")
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 5)
            self.assertEqual(self.executor.cancel_calls, [live["attempt_id"]])
            result = await asyncio.wait_for(worker, 5)
            self.assertTrue(result["process_tree_terminated"])
            self.assertEqual(self.ledger.attempt(live["attempt_id"])["state"], "RELEASED")
            current = self.ledger.attempt(unknown["attempt_id"])
            self.assertEqual(current["state"], "STOPPING")
            self.assertTrue(current["cancel_requested"])
            self.assertIsNone(current["outcome"])
            self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
            self.assertEqual(len(self.releases()), 1)
            # A later duplicate exit cannot release the unknown sibling's slot.
            self.controller._observe_exit(live, result)
            self.assertEqual(len(self.releases()), 1)
            self.assertEqual(self.ledger.status()["capacity"]["used"], 1)
            self.assertEqual(self.ledger.task("project", "queued")["state"], "CANCELLED")
            self.assertFalse(self.ledger.project("project")["enabled"])
            self.assertEqual(closed, [True])
            self.assertEqual(self.executor.run_order, ["live"])
            self.assertEqual(self.controller.errors, [])
        finally:
            recovery_block.set()
            for release in self.executor.release.values():
                release.set()
            if running is not None and not running.done():
                running.cancel()
            await asyncio.gather(worker, *([running] if running is not None else []), return_exceptions=True)

    async def test_deadline_cancels_active_trees_without_revoking_review_or_queued_work(self):
        _, candidate = self.fixture.candidate("finished")
        accepted = self.accept_candidate("finished", candidate)
        finished = self.ledger.task("project", "finished")
        for name in ("active-first", "active-second", "queued"):
            self.fixture.add(name)
            self.executor.started[name] = asyncio.Event()
            self.executor.release[name] = asyncio.Event()
            self.executor.done[name] = asyncio.Event()
        queued = self.ledger.task("project", "queued")
        running = asyncio.create_task(self.controller.run("project", timeout=0.5, watch=True))
        try:
            await self.wait(self.executor.started["active-first"])
            await self.wait(self.executor.started["active-second"])
            self.assertFalse(self.executor.started["queued"].is_set())
            result = await asyncio.wait_for(running, 5)
        finally:
            for release in self.executor.release.values():
                release.set()
            await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(result["stop_reason"], "timeout_or_cancel")
        self.assertEqual(result["errors"], [])
        self.assertTrue(self.ledger.project("project")["enabled"])
        self.assertEqual(self.ledger.task("project", "queued"), queued)
        self.assertEqual(self.ledger.task("project", "finished"), finished)
        self.assertEqual(self.ledger.gate("project", "finished", "final_accepted",
                                          accepted["artifact_digest"]), accepted)
        self.assertEqual(self.executor.run_order, ["active-first", "active-second"])
        self.assertEqual(len(self.executor.cancel_calls), 2)
        self.assertEqual(result["status"]["capacity"]["used"], 0)
        self.assertEqual(len(self.releases()), 3)  # Includes the previously finished candidate.
        for identifier in self.executor.cancel_calls:
            current = self.ledger.attempt(identifier)
            self.assertEqual(current["state"], "RELEASED")
            self.assertTrue(current["outcome"]["process_tree_terminated"])
            self.assertTrue(current["outcome"]["cancelled"])

    async def test_deadline_rechecks_sibling_that_finishes_during_earlier_cancellation(self):
        for name in ("cancel-first", "finishes-during-cancel"):
            self.fixture.add(name)
            self.executor.started[name] = asyncio.Event()
            self.executor.release[name] = asyncio.Event()
            self.executor.done[name] = asyncio.Event()
        published = asyncio.Event()
        accepted = {}
        original_finish = self.pipeline.finish
        async def publish(attempt, job, result):
            await original_finish(attempt, job, result)
            if attempt["task_id"] == "finishes-during-cancel":
                candidate = {"commit": "2" * 40, "artifact_digest": fixtures.digest("racing-candidate"),
                             "plan": attempt["task"]["plan"], "changed_paths": []}
                self.ledger.submit_candidate(attempt["attempt_id"], attempt["generation"], candidate,
                                             fixtures.EXEC, self.fixture.key())
                accepted.update(self.accept_candidate(attempt["task_id"], candidate))
                published.set()
        self.pipeline.finish = publish
        original_cancel = self.executor.cancel
        async def delayed_cancel(identifier, grace=2.0):
            if self.executor.by_attempt[identifier] == "cancel-first":
                self.executor.release["finishes-during-cancel"].set()
                await self.wait(published)
            return await original_cancel(identifier, grace)
        self.executor.cancel = delayed_cancel
        running = asyncio.create_task(self.controller.run("project", timeout=0.5, watch=True))
        try:
            await self.wait(self.executor.started["cancel-first"])
            await self.wait(self.executor.started["finishes-during-cancel"])
            result = await asyncio.wait_for(running, 5)
        finally:
            published.set()
            for release in self.executor.release.values():
                release.set()
            await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"]["capacity"]["used"], 0)
        self.assertEqual(len(self.executor.cancel_calls), 1)
        self.assertEqual(self.executor.by_attempt[self.executor.cancel_calls[0]], "cancel-first")
        finished = self.ledger.task("project", "finishes-during-cancel")
        self.assertEqual(finished["state"], "ACCEPTED")
        self.assertFalse(finished["cancel_requested"])
        self.assertEqual(self.ledger.gate("project", "finishes-during-cancel", "final_accepted",
                                          accepted["artifact_digest"]), accepted)
        self.assertTrue(self.ledger.project("project")["enabled"])
        self.assertEqual(len(self.releases()), 2)

    async def test_deadline_keeps_unknown_exit_charged_and_other_project_untouched(self):
        self.fixture.project("other", self.fixture.home / "other")
        self.ledger.register_plan("other", self.fixture.make_plan(), fixtures.PLAN, self.fixture.key())
        self.fixture.add("outside", project_id="other")
        outside = self.fixture.launch("outside", project_id="other")
        self.fixture.add("uncertain")
        unknown = self.ledger.reserve("project", "uncertain", fixtures.COORD, self.fixture.key())
        self.ledger.claim_launch(unknown["attempt_id"], 1, fixtures.EXEC, self.fixture.key())
        self.fixture.add("queued")
        queued = self.ledger.task("project", "queued")
        result = await self.controller.run("project", timeout=0.05, watch=True)
        self.assertEqual(result["stop_reason"], "timeout_or_cancel")
        self.assertEqual(result["status"]["capacity"]["used"], 2)
        self.assertEqual(self.ledger.attempt(outside["attempt_id"]), outside)
        self.assertEqual(self.ledger.task("project", "queued"), queued)
        current = self.ledger.attempt(unknown["attempt_id"])
        self.assertEqual(current["state"], "STOPPING")
        self.assertTrue(current["cancel_requested"])
        self.assertIsNone(current["outcome"])
        self.assertEqual(self.executor.run_order, [])
        self.assertEqual(self.executor.cancel_calls, [])
        self.assertEqual(self.releases(), [])
        self.assertTrue(self.ledger.project("project")["enabled"])
        self.assertTrue(self.ledger.project("other")["enabled"])

    async def test_watch_timeout_is_bounded_while_idle(self):
        result = await asyncio.wait_for(self.controller.run(timeout=0.05, watch=True), 1)
        self.assertEqual(result["stop_reason"], "timeout_or_cancel")
        self.assertTrue(result["watch"])
        self.assertEqual(result["errors"], [])
        self.assertTrue(self.ledger.project("project")["enabled"])

    async def test_watch_flag_and_timeout_require_exact_types(self):
        for options in ({"watch": "yes"}, {"timeout": True}, {"timeout": 10**400},
                        {"timeout": float("inf")}, {"timeout": float("nan")}):
            with self.subTest(options=options), self.assertRaises(Fault) as caught:
                await self.controller.run(**options)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
