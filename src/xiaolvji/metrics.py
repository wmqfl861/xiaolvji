"""Bounded, read-only observations from one durable controller snapshot.

These are measurements of the controller's records, not authorization or proof
that a task was correct. Registration wait includes dependency and retry waits;
release-to-reservation gaps include idle time and do not identify a physical slot.
No worker files, provider credentials, command output, or raw task text are read.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime
import math

from .contracts import Fault, load_json, utc_now, validate_identifier
from .store import ACTIVE_SQL, GATES

MAX_EVENTS = 100_000
MAX_TASKS = 10_000
MAX_ATTEMPTS = 20_000
MAX_DETAIL_BYTES = 16 * 1024 * 1024
_PAYLOAD_EVENTS = ("run_exit_confirmed", "run_cancel_confirmed", "run_start_failed",
                   "gate_recorded", "review_recorded")


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = parsed.timestamp()
        if parsed.tzinfo is None or not math.isfinite(result):
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise Fault("METRICS_INCONSISTENT", "Controller history contains an invalid timestamp.") from exc


def _distribution(values: list[float]) -> dict:
    # A backwards wall clock must not become a negative latency or a fabricated
    # zero. Keep an explicit exclusion count; ordering still follows event seq.
    ordered = sorted(value for value in values if value >= 0)
    count = len(ordered)
    return {"samples": count, "excluded_clock_reversals": len(values) - count,
            "min": round(ordered[0], 6) if count else None,
            "p50": round(ordered[math.ceil(count * .5) - 1], 6) if count else None,
            "p95": round(ordered[math.ceil(count * .95) - 1], 6) if count else None,
            "max": round(ordered[-1], 6) if count else None,
            "mean": round(math.fsum(ordered) / count, 6) if count else None}


def runtime_metrics(ledger, project_id: str | None = None) -> dict:
    """Summarize complete retained history in one read transaction.

    The authenticated CLI/MCP entry point owns read authority. Limits are fixed,
    not caller-controlled. Oversized histories fail explicitly, never silently
    truncate a population and label it complete. A project filter affects the
    measurements; global occupancy always includes every enrolled project.
    """
    if project_id is not None:
        validate_identifier(project_id, "project_id")
    selected = " WHERE project_id=?" if project_id is not None else ""
    args = (project_id,) if project_id is not None else ()
    with closing(ledger._connect()) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        # The first read establishes the snapshot before sampling its wall time.
        cursor = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        observed_at = utc_now()
        now = _timestamp(observed_at)
        if project_id is not None and not conn.execute("SELECT 1 FROM projects WHERE id=?", args).fetchone():
            raise Fault("NOT_FOUND", "Unknown project")
        counts = {}
        for table, bound in (("events", MAX_EVENTS), ("tasks", MAX_TASKS), ("attempts", MAX_ATTEMPTS)):
            counts[table] = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM " + table + selected
                                         + " LIMIT ?)", (*args, bound + 1)).fetchone()[0]
            if counts[table] > bound:
                raise Fault("METRICS_LIMIT", "Complete metrics exceed the bounded snapshot budget; select a smaller project.",
                            {"population": table, "at_least": counts[table], "limit": bound})
        global_limit = int(conn.execute("SELECT value FROM settings WHERE key='global_limit'").fetchone()[0])
        global_used, global_uncertain = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(state='LOST'),0) FROM attempts WHERE state IN {ACTIVE_SQL}"
        ).fetchone()
        task_states = dict(conn.execute("SELECT state,COUNT(*) FROM tasks" + selected + " GROUP BY state", args))
        attempt_states = dict(conn.execute("SELECT state,COUNT(*) FROM attempts" + selected + " GROUP BY state", args))
        task_rows = {(row["project_id"], row["task_id"]): row["created_seq"] for row in conn.execute(
            "SELECT project_id,task_id,created_seq FROM tasks" + selected, args)}
        attempt_rows = {row["attempt_id"]: row for row in conn.execute(
            "SELECT attempt_id,project_id,task_id,state,created_seq,identity IS NOT NULL AS has_identity FROM attempts"
            + selected, args)}
        retried_tasks = conn.execute("SELECT COUNT(*) FROM (SELECT project_id,task_id FROM attempts" + selected
                                    + " GROUP BY project_id,task_id HAVING COUNT(*)>1)", args).fetchone()[0]
        registration, first_reservation, reserved, started, released = {}, {}, {}, {}, {}
        terminal_attempts = set()
        pending_releases, refill_gaps = [], []
        registration_wait, occupied_time, execution_time = [], [], []
        terminal = Counter({key: 0 for key in ("succeeded", "failed", "timed_out", "cancelled", "start_failed")})
        gates = Counter({key: 0 for key in GATES})
        reviews = Counter({key: 0 for key in ("approve", "reject", "rework")})
        current, peak, clock_reversals = 0, 0, 0
        first_time, last_time, previous_time = None, None, None
        # Fetch payload only for the five event kinds whose typed fields are
        # needed. Objectives, source contents and instruction text are not read.
        placeholders = ",".join("?" for _ in _PAYLOAD_EVENTS)
        detail_bytes = conn.execute(
            "SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM events "
            f"WHERE type IN ({placeholders})" + (" AND project_id=?" if project_id is not None else ""),
            (*_PAYLOAD_EVENTS, *args)).fetchone()[0]
        if detail_bytes > MAX_DETAIL_BYTES:
            raise Fault("METRICS_LIMIT", "Complete metrics exceed the bounded detail budget; select a smaller project.",
                        {"population": "event_detail_bytes", "count": detail_bytes, "limit": MAX_DETAIL_BYTES})
        sql = ("SELECT seq,type,project_id,task_id,attempt_id,occurred_at,"
               f"CASE WHEN type IN ({placeholders}) THEN payload ELSE NULL END AS detail "
               "FROM events" + selected + " ORDER BY seq")
        for row in conn.execute(sql, (*_PAYLOAD_EVENTS, *args)):
            stamp = _timestamp(row["occurred_at"])
            if previous_time is not None and stamp < previous_time:
                clock_reversals += 1
            previous_time = last_time = stamp
            if first_time is None:
                first_time = stamp
            kind, attempt_id = row["type"], row["attempt_id"]
            task_key = (row["project_id"], row["task_id"])
            payload = {}
            if row["detail"] is not None:
                try:
                    payload = load_json(row["detail"])
                    if not isinstance(payload, dict):
                        raise ValueError
                except (Fault, ValueError):
                    raise Fault("METRICS_INCONSISTENT", "Controller history contains an invalid measurement record.") from None
            if kind == "task_registered":
                if task_key in registration or task_rows.get(task_key) != row["seq"]:
                    raise Fault("METRICS_INCONSISTENT", "Task registration history does not match durable tasks.")
                registration[task_key] = stamp
            elif kind == "run_reserved":
                if attempt_id in reserved or attempt_id not in attempt_rows:
                    raise Fault("METRICS_INCONSISTENT", "Reservation history does not match durable attempts.")
                stored = attempt_rows[attempt_id]
                if (stored["created_seq"] != row["seq"] or (stored["project_id"], stored["task_id"]) != task_key
                        or task_key not in registration):
                    raise Fault("METRICS_INCONSISTENT", "Reservation history does not match its registered task.")
                reserved[attempt_id] = stamp
                current += 1
                peak = max(peak, current)
                if task_key not in first_reservation:
                    first_reservation[task_key] = stamp
                    if task_key in registration:
                        registration_wait.append(stamp - registration[task_key])
                refill_gaps.extend(stamp - released_at for released_at in pending_releases)
                pending_releases.clear()
            elif kind == "run_start_ack":
                # A reconciled start acknowledgment may be repeated. Only the
                # first observation starts the measured interval.
                if attempt_id not in reserved or attempt_id in released:
                    raise Fault("METRICS_INCONSISTENT", "Start history does not match an active reservation.")
                started.setdefault(attempt_id, stamp)
            elif kind == "resource_released":
                if attempt_id not in reserved or attempt_id in released or attempt_id not in terminal_attempts:
                    raise Fault("METRICS_INCONSISTENT", "Release history does not match reservations.")
                released[attempt_id] = stamp
                current -= 1
                occupied_time.append(stamp - reserved[attempt_id])
                pending_releases.append(stamp)
            elif kind in {"run_exit_confirmed", "run_cancel_confirmed", "run_start_failed"}:
                if attempt_id not in reserved or attempt_id in terminal_attempts:
                    raise Fault("METRICS_INCONSISTENT", "Terminal history does not match an active reservation.")
                terminal_attempts.add(attempt_id)
                observation = payload.get("observation")
                if not isinstance(observation, dict):
                    raise Fault("METRICS_INCONSISTENT", "Terminal history lacks a structured observation.")
                if kind == "run_start_failed":
                    terminal["start_failed"] += 1
                elif kind == "run_cancel_confirmed" or observation.get("cancelled") is True:
                    terminal["cancelled"] += 1
                elif observation.get("timed_out") is True:
                    terminal["timed_out"] += 1
                elif type(observation.get("exit_code")) is int:
                    terminal["succeeded" if observation["exit_code"] == 0 else "failed"] += 1
                else:
                    raise Fault("METRICS_INCONSISTENT", "Terminal history lacks a valid exit status.")
                if attempt_id in started:
                    execution_time.append(stamp - started[attempt_id])
            elif kind in {"gate_recorded", "review_recorded"}:
                gate = payload.get("gate")
                if isinstance(gate, str) and gate in gates:
                    gates[gate] += 1
                decision = "approve" if gate == "final_accepted" else payload.get("decision")
                if kind == "review_recorded" and isinstance(decision, str) and decision in reviews:
                    reviews[decision] += 1
        if (set(registration) != set(task_rows) or set(reserved) != set(attempt_rows)
                or any((row["state"] == "RELEASED") != (attempt_id in released)
                       or bool(row["has_identity"]) != (attempt_id in started) for attempt_id, row in attempt_rows.items())
                or set(released) != terminal_attempts):
            raise Fault("METRICS_INCONSISTENT", "Lifecycle history and current attempts disagree.")
        active_age = [now - stamp for attempt_id, stamp in reserved.items() if attempt_id not in released]
        return {
            "schema_version": 1, "diagnostic_only": True, "event_cursor": cursor,
            "project_id": project_id, "observed_at": observed_at,
            "history": {"complete": True, "events": counts["events"], "first_event_at_epoch": first_time,
                        "last_event_at_epoch": last_time, "timestamp_basis": "controller_wall_clock",
                        "timestamp_order_reversals": clock_reversals,
                        "detail_bytes": detail_bytes,
                        "limits": {"events": MAX_EVENTS, "tasks": MAX_TASKS, "attempts": MAX_ATTEMPTS,
                                   "event_detail_bytes": MAX_DETAIL_BYTES}},
            "capacity": {"global": {"used": global_used, "limit": global_limit,
                                    "available": max(0, global_limit - global_used), "uncertain": global_uncertain},
                         "selected_active": current, "selected_uncertain": attempt_states.get("LOST", 0),
                         "selected_peak_reserved": peak},
            "population": {"tasks": counts["tasks"], "stored_task_states": task_states,
                           "attempts": counts["attempts"], "attempt_states": attempt_states,
                           "retried_tasks": retried_tasks,
                           "retry_attempts": counts["attempts"] - len(first_reservation),
                           "tasks_ever_reserved": len(first_reservation)},
            "process_outcomes": dict(terminal),
            "seconds": {
                "registration_to_first_reservation": _distribution(registration_wait),
                "reservation_to_release": _distribution(occupied_time),
                "first_start_ack_to_terminal_observation": _distribution(execution_time),
                "release_to_next_reservation_in_scope": _distribution(refill_gaps),
                "active_reservation_age": _distribution(active_age)},
            "slot_usage": {"closed_reservation_seconds": round(math.fsum(x for x in occupied_time if x >= 0), 6),
                           "active_reservation_seconds": round(math.fsum(x for x in active_age if x >= 0), 6),
                           "active_intervals_are_incomplete": bool(active_age),
                           "releases_without_later_reservation": len(pending_releases)},
            "historical_quality_records": {"gate_records": dict(gates), "review_decisions": dict(reviews),
                                           "records_may_have_been_revoked": True},
            "unavailable": {
                "eligible_queue_wait": "Eligibility transitions are not durably timestamped; registration wait includes dependencies and backoff.",
                "pure_dispatch_latency": "A release-to-reservation gap can include idle time and does not associate a particular physical slot.",
                "delivery_wall_time": "Tasks may arrive continuously; there is no immutable delivery cohort or final delivery checkpoint.",
                "technical_first_pass_rate": "Process success and recorded gates do not enumerate every required check attempt.",
                "integration_conflict_rate": "Historical task blockers do not carry a stable conflict classification.",
                "cpu_memory_and_model_usage": "The ledger does not record complete process-tree CPU, memory, or provider usage."},
        }
