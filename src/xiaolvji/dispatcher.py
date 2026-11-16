"""Event-driven rolling dispatch over an authoritative SQLite ledger.

The pipeline prepares a private job and publishes candidates only after an
executor proves tree exit. A model message never releases a slot.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
import uuid
from pathlib import Path

from .contracts import Fault, Principal, read_json

COORDINATOR = Principal("controller-coordinator", "coordinator")
EXECUTOR = Principal("controller-executor", "executor")


def request_id(prefix: str) -> str:
    return prefix + ":" + uuid.uuid4().hex


class Dispatcher:
    def __init__(self, ledger, executor, pipeline, event_callback=None):
        self.ledger, self.executor, self.pipeline = ledger, executor, pipeline
        self.event_callback = event_callback
        self._pending: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self.errors: list[dict] = []
        self._stopping = False
        self._last_cursor = 0
        self._instance_id = uuid.uuid4().hex

    def _emit(self, event: dict):
        if self.event_callback:
            self.event_callback(event)

    def _error(self, attempt: dict | None, error: Exception):
        item = {"attempt_id": attempt.get("attempt_id") if attempt else None,
                "error": error.as_dict() if isinstance(error, Fault) else
                    {"code": "INTERNAL_ERROR", "message": str(error)}}
        self.errors.append(item)
        self._emit({"type": "controller_error", **item})

    async def _run_one(self, attempt: dict):
        identifier, generation = attempt["attempt_id"], attempt["generation"]
        started = False
        claimed = False
        entered_executor = False
        job = None
        try:
            project = self.ledger.project(attempt["project_id"])
            if project["config"]["profile"] != self.executor.profile:
                raise Fault("CAPABILITY_UNSUPPORTED", "Attempt profile differs from this execution gateway.")
            if not project["enabled"]:
                raise Fault("DEPENDENCY_BLOCKED", "Project is disabled; its reservation remains paused.")
            attempt = self.ledger.claim_launch(identifier, generation, EXECUTOR, "claim:" + self._instance_id + ":" + identifier)
            claimed = True
            if attempt["cancel_requested"]:
                raise Fault("CANCELLED", "Attempt was cancelled before startup.")
            job = await self.pipeline.prepare(attempt)
            # Cancellation may arrive while a large private clone is prepared.
            current = self.ledger.attempt(identifier)
            if current["cancel_requested"]:
                raise Fault("CANCELLED", "Attempt was cancelled during preparation.")
            if not current["project"]["enabled"] or current["project"]["config"]["profile"] != self.executor.profile:
                raise Fault("CAPABILITY_UNSUPPORTED", "Project execution policy changed during preparation.")
            async def on_started(identity):
                nonlocal started
                acknowledged = self.ledger.observe(identifier, generation, "started", {"identity": identity},
                                                   EXECUTOR, "start:" + identifier)
                started = True
                # Preflight may await a slow clone scan/probe. Cancellation or
                # project disablement during that gap must still prevent the
                # supervisor from receiving the command authorization.
                if acknowledged["cancel_requested"]:
                    raise Fault("CANCELLED", "Attempt was cancelled before command authorization.")
                if (not acknowledged["project"]["enabled"] or
                        acknowledged["project"]["config"]["profile"] != self.executor.profile):
                    raise Fault("CAPABILITY_UNSUPPORTED", "Project policy changed before command authorization.")
                if hasattr(self.pipeline, "started"):
                    await self.pipeline.started(attempt)
                self._emit({"type": "started", "task_id": attempt["task_id"], "attempt_id": identifier})
                self._wake.set()
            entered_executor = True
            if job.get("trusted_check_service"):
                from .quality_service import run_quality_service
                result = await run_quality_service(self.executor, attempt, job, on_started=on_started)
            else:
                options = {"sandbox_policy": job["sandbox_policy"]} if "sandbox_policy" in job else {}
                if "provider_runtime" in job:
                    options["provider_runtime"] = job["provider_runtime"]
                result = await self.executor.run(attempt, job["argv"], job["cwd"], env=job.get("env"),
                                                 stdin_text=job.get("stdin_text"), timeout=job.get("timeout", 3600),
                                                 on_started=on_started, **options)
            if result.get("process_tree_terminated") is not True:
                self.ledger.observe(identifier, generation, "lost", {"reason": result.get("reason", "Tree exit is unconfirmed")},
                                    EXECUTOR, request_id("lost"))
                raise Fault("INSTANCE_UNCERTAIN", "Execution remains charged until tree exit is proven.")
            # Release before snapshot/verification preparation; wake the queue immediately.
            self._observe_exit(attempt, result)
            self._wake.set()
            if not self.ledger.attempt(identifier)["cancel_requested"]:
                if result.get("exit_code") == 0 and not result.get("timed_out") and not result.get("cancelled"):
                    await self.pipeline.finish(attempt, job, result)
                else:
                    await self.pipeline.failed(attempt, job, result)
            self._emit({"type": "completed", "task_id": attempt["task_id"], "attempt_id": identifier,
                        "exit_code": result.get("exit_code"), "cancelled": result.get("cancelled", False)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error(attempt, exc)
            if not claimed:
                # Another dispatcher/owner may have the launch. Failure to
                # acquire ownership never authorizes a lifecycle observation.
                return
            current = self.ledger.attempt(identifier)
            if current["state"] != "RELEASED":
                recovered = None
                if entered_executor and current.get("identity"):
                    try:
                        observed = self.executor.reconcile(current["identity"])
                        if observed.get("process_tree_terminated") is True:
                            recovered = observed
                    except Fault:
                        pass  # Missing/unbound evidence must remain charged.
                if recovered is not None:
                    self._observe_exit(attempt, recovered)
                    if not current["cancel_requested"] and job is not None:
                        await self.pipeline.failed(attempt, job, recovered)
                    return
                details = getattr(exc, "details", None) or {}
                never_started = not entered_executor or (isinstance(exc, Fault) and
                    (exc.code == "CAPABILITY_UNSUPPORTED" or details.get("no_process_created") is True))
                if never_started and not started:
                    self.ledger.observe(identifier, generation, "start_failed",
                                        {"reason": str(exc), "no_process_created": True}, EXECUTOR, request_id("not-started"))
                else:
                    self.ledger.observe(identifier, generation, "lost", {"reason": str(exc)}, EXECUTOR, request_id("lost"))
            elif current.get("exit_code") == 0:
                # A failed scope audit/check report cannot appear as technical readiness.
                self.ledger.block_task(attempt["project_id"], attempt["task_id"], str(exc), COORDINATOR,
                                       request_id("postprocessing-blocked"))
        finally:
            if hasattr(self.pipeline, "release"):
                try:
                    await self.pipeline.release(attempt)
                except Exception as exc:
                    self._error(attempt, exc)
            self._wake.set()

    def _observe_exit(self, attempt, result):
        payload = {"identity": result["identity"], "exit_code": result["exit_code"], "process_tree_terminated": True}
        for key in ("timed_out", "cancelled"):
            if key in result:
                payload[key] = result[key]
        self.ledger.observe(attempt["attempt_id"], attempt["generation"], "exited", payload,
                            EXECUTOR, "exit:" + attempt["attempt_id"])

    def _block_delivery(self, attempt, exc):
        self._error(attempt, exc)
        try:
            self.ledger.block_task(attempt["project_id"], attempt["task_id"], str(exc)[:8000] or "Recovery failed",
                                   COORDINATOR, request_id("recovery-blocked"))
        except Fault as failure:
            # A superseded/cancelled task remains superseded/cancelled.
            self._error(attempt, failure)

    async def reconcile(self, project_id: str | None = None):
        for attempt in self.ledger.active_attempts(project_id):
            identifier = attempt["attempt_id"]
            if identifier in self._pending or attempt["state"] == "RESERVED":
                continue
            if attempt.get("launch_state") == "CANCELLED" and not attempt.get("identity"):
                self.ledger.observe(identifier, attempt["generation"], "start_failed",
                    {"reason": "Pending launch atomically cancelled before any claim", "no_process_created": True},
                    EXECUTOR, "cancel-pending:" + identifier)
                continue
            try:
                identity = attempt.get("identity")
                if not identity:
                    p = Path(self.executor.state_dir) / "launches" / identifier / "identity.json"
                    if p.is_file() and not p.is_symlink():
                        candidate = read_json(p)
                        if candidate.get("attempt_id") == identifier and candidate.get("generation") == attempt["generation"]:
                            identity = candidate
                            self.ledger.observe(identifier, attempt["generation"], "started", {"identity": identity},
                                                EXECUTOR, "start:" + identifier)
                if identity:
                    result = self.executor.reconcile(identity)
                    if result.get("process_tree_terminated") is True:
                        self._observe_exit(attempt, result)
                        try:
                            await self.pipeline.recover_attempt(attempt, result)
                        except Exception as exc:
                            self._block_delivery(attempt, exc)
                        continue  # A proven exit can never become LOST.
                    if result.get("status") == "live":
                        continue
            except Fault as exc:
                self._error(attempt, exc)
            if self.ledger.attempt(identifier)["state"] not in {"RELEASED", "LOST"}:
                self.ledger.observe(identifier, attempt["generation"], "lost",
                    {"reason": "No durable process-tree exit attestation; reconciliation required"},
                    EXECUTOR, request_id("uncertain"))
        recovered_errors = await self.pipeline.recover_completed(project_id)
        for item in recovered_errors or []:
            self._error(None, Fault("RECOVERY_BLOCKED", item["message"], item))

    async def cancel(self, project_id: str, task_id: str | None = None):
        affected = self.ledger.cancel(project_id, task_id, COORDINATOR, request_id("cancel"))["attempts"]
        results = []
        for attempt in affected:
            # A reserved task that was never claimed is known to have no launch.
            journal = Path(self.executor.state_dir) / "launches" / attempt["attempt_id"]
            if attempt.get("launch_state") == "CANCELLED" and not attempt.get("identity"):
                self.ledger.observe(attempt["attempt_id"], attempt["generation"], "start_failed",
                    {"reason": "Pending launch atomically cancelled before any claim", "no_process_created": True},
                    EXECUTOR, "cancel-pending:" + attempt["attempt_id"])
                results.append({"attempt_id": attempt["attempt_id"], "status": "cancelled_before_launch"})
                continue
            if not attempt.get("identity") and not journal.exists() and attempt["attempt_id"] not in self._pending:
                results.append({"attempt_id": attempt["attempt_id"], "status": "uncertain"})
                continue
            result = await self.executor.cancel(attempt["attempt_id"])
            if result.get("process_tree_terminated") is True:
                self._observe_exit(attempt, result)
            results.append({"attempt_id": attempt["attempt_id"], **result})
        self._wake.set()
        return results

    async def _cancel_active_attempts(self, project_id: str | None):
        # A controller deadline ends execution, not the project's queued work
        # or already accepted evidence. Each task cancellation may await tree
        # exit while another attempt finishes and publishes its candidate.
        for attempt in self.ledger.active_attempts(project_id):
            current = self.ledger.attempt(attempt["attempt_id"])
            if current["state"] in {"RESERVED", "STARTING", "RUNNING", "STOPPING", "LOST"}:
                await self.cancel(current["project_id"], current["task_id"])

    async def run(self, project_id: str | None = None, timeout: float = 3600, *, watch: bool = False) -> dict:
        # One dispatcher owns a domain's queue and publication side effects.
        # SQLite still enforces atomic admission across all client connections.
        try:
            import fcntl
        except ImportError as exc:
            raise Fault("CAPABILITY_UNSUPPORTED", "This runtime needs POSIX controller locking.") from exc
        lock_path = Path(self.executor.state_dir).parent / "dispatcher.lock"
        if lock_path.is_symlink():
            raise Fault("UNSAFE_STATE", "Controller lock cannot be a symlink.")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Fault("CONTROLLER_RUNNING", "Another dispatcher already owns this resource domain.") from exc
            try:
                return await self._run_owned(project_id, timeout, watch=watch)
            finally:
                try:
                    if hasattr(self.pipeline, "close"):
                        await self.pipeline.close()
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def _run_owned(self, project_id: str | None = None, timeout: float = 3600, *, watch: bool = False) -> dict:
        if (type(timeout) not in (int, float) or timeout <= 0 or timeout > sys.float_info.max
                or not math.isfinite(timeout)):
            raise Fault("INVALID_ARGUMENT", "Controller timeout must be positive and finite.")
        if type(watch) is not bool:
            raise Fault("INVALID_ARGUMENT", "Watch mode must be an explicit boolean.")
        probe = self.executor.probe()
        if not probe.get("supported"):
            raise Fault("CAPABILITY_UNSUPPORTED", "Execution profile is unavailable.", probe)
        start = time.monotonic()
        waiting = False
        try:
            # Recovery can await postprocessing while durable launches remain
            # live. Cancellation must cover this initial ownership phase too.
            await self.reconcile(project_id)
            while not self._stopping:
                self._wake.clear()
                for identifier, task in list(self._pending.items()):
                    if task.done():
                        try:
                            await task
                        except Exception as exc:
                            self._error(None, exc)
                        del self._pending[identifier]
                # Recovery and postprocessing can consume the remaining time.
                # Never admit one more batch after the controller deadline.
                if time.monotonic() - start >= timeout:
                    self._stopping = True
                    await self._cancel_active_attempts(project_id)
                    break
                # Resume unclaimed durable outbox entries after a controller restart.
                for attempt in self.ledger.launches():
                    if ((project_id is None or attempt["project_id"] == project_id) and
                            attempt["attempt_id"] not in self._pending and attempt["project"]["enabled"] and
                            attempt["project"]["config"]["profile"] == self.executor.profile):
                        self._pending[attempt["attempt_id"]] = asyncio.create_task(self._run_one(attempt))
                if await self.pipeline.discover(project_id):
                    self._wake.set()
                for item in self.ledger.ready_tasks(project_id):
                    if time.monotonic() - start >= timeout:
                        break
                    project = self.ledger.project(item["project_id"])
                    if project["config"]["profile"] != self.executor.profile:
                        continue
                    try:
                        attempt = self.ledger.reserve(item["project_id"], item["task_id"], COORDINATOR, request_id("admit"))
                    except Fault as exc:
                        if exc.code == "CAPACITY_FULL":
                            # A project's cap can be full while another fits.
                            if self.ledger.status()["capacity"]["available"] == 0:
                                break
                            continue
                        if exc.code in {"SCOPE_CONFLICT", "DEPENDENCY_BLOCKED", "CONTRACT_STALE", "CANCELLED"}:
                            continue
                        self._error(None, exc)
                        continue
                    self._pending[attempt["attempt_id"]] = asyncio.create_task(self._run_one(attempt))
                status = self.ledger.status(project_id)
                if status["event_cursor"] != self._last_cursor:
                    self._last_cursor = status["event_cursor"]
                    self._emit({"type": "capacity", **status["capacity"], "event_cursor": self._last_cursor})
                active = status["active_attempts"]
                future_retry = [t["not_before"] for t in status["tasks"]
                                if t["blocked_reason"] == "RETRY_BACKOFF" and t["not_before"] > time.time()]
                if not self._pending and not active:
                    if not future_retry and not watch:
                        break
                if time.monotonic() - start >= timeout:
                    self._stopping = True
                    await self._cancel_active_attempts(project_id)
                    break
                # A durable launch owned by another process is monitored. LOST
                # instances pause immediately; they never cause a busy retry loop.
                if not self._pending and active:
                    if not watch and all(a["state"] == "LOST" or
                           (a["state"] == "RESERVED" and (not a["project"]["enabled"] or
                            a["project"]["config"]["profile"] != self.executor.profile)) for a in active):
                        break
                    await self.reconcile(project_id)
                idle = not self._pending
                if watch and idle and not waiting:
                    self._emit({"type": "idle_waiting", "project_id": project_id,
                                "event_cursor": status["event_cursor"],
                                "occupied": status["capacity"]["used"],
                                "uncertain": status["capacity"]["uncertain"]})
                waiting = idle
                delay = min(0.25, max(0.01, min(future_retry) - time.time())) if future_retry else 0.25
                if not watch and not self._pending and not active and future_retry:
                    delay = min(10, max(0.01, min(future_retry) - time.time()))
                delay = min(delay, max(0.001, timeout - (time.monotonic() - start)))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            if self._pending:
                await asyncio.gather(*self._pending.values(), return_exceptions=True)
            final = self.ledger.status(project_id)
            return {"status": final, "errors": self.errors, "watch": watch,
                    "elapsed_seconds": round(time.monotonic()-start, 3),
                    "stop_reason": "timeout_or_cancel" if self._stopping else
                        "uncertain_instances" if final["capacity"]["uncertain"] else "no_eligible_work"}
        except (asyncio.CancelledError, KeyboardInterrupt):
            self._stopping = True
            for project in self.ledger.status(project_id)["projects"]:
                await asyncio.shield(self.cancel(project["project_id"]))
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
            raise
