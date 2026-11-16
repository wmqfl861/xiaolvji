"""Pure, restart-stable queue ordering; never an admission or quality authority.

Registration age intentionally includes dependency and retry-backoff waits. A
task's priority plus quality-role preference ages toward a shared finite ceiling;
FIFO breaks ties there, so newer high-priority arrivals cannot starve an older
eligible task forever. Capacity, conflicts, evidence and retries still gate every
reservation. This policy does not promise a wall-clock start deadline.
"""
from __future__ import annotations

from datetime import datetime

DEFAULT_PRIORITY = 50
ROLE_BONUS = {"verifier": 20, "integrator": 10}
AGING_STEP_SECONDS = 30
RANK_CEILING = 120


def policy() -> dict:
    """Return an independent JSON description, safe for diagnostic callers."""
    return {
        "name": "bounded_priority_aging",
        "version": 1,
        "default_priority": DEFAULT_PRIORITY,
        "role_bonus": dict(ROLE_BONUS),
        "aging_step_seconds": AGING_STEP_SECONDS,
        "rank_ceiling": RANK_CEILING,
        "age_source": "task_registration",
        "age_includes_dependency_and_backoff_wait": True,
        "maximum_seconds_to_ceiling": RANK_CEILING * AGING_STEP_SECONDS,
        "start_deadline_guaranteed": False,
        "order": ["eligible_desc", "effective_rank_desc", "created_seq_asc",
                  "project_id_asc", "task_id_asc"],
    }


def rank(definition: dict, registered_at: str, now: float) -> dict:
    """Rank one validated task at a caller-sampled instant, without reading time."""
    priority = definition.get("priority", DEFAULT_PRIORITY)
    bonus = ROLE_BONUS.get(definition["role"], 0)
    base = priority + bonus
    parsed = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Task registration time must include a timezone")
    registered = parsed.timestamp()
    age = max(0, int(now - registered))
    credit = min(RANK_CEILING - base, age // AGING_STEP_SECONDS)
    return {"priority": priority, "role_bonus": bonus, "base_rank": base,
            "effective_rank": base + credit, "aging_credit": credit,
            "waiting_age_seconds": age}


def order_key(effective_rank: int, created_seq: int,
              project_id: str, task_id: str) -> tuple:
    return (-effective_rank, created_seq, project_id, task_id)
