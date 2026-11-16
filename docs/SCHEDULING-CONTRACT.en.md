# Scheduling and diagnostic contract — 0.5.0

This additive contract describes bounded priority aging, queue admission previews,
and controller-history observations. Existing plan, gate, principal, scope, lease,
generation and process-tree exit requirements remain authoritative. The JSON task
schema and database schema stay at version 1; no migration or new privilege is
required.

## Ordering

`scheduling.py` exports `policy()`, `rank(definition, registered_at, now)` and
`order_key(effective_rank, created_seq, project_id, task_id)`. Tasks use their
validated priority (0–100, default 50), plus role bonuses of 20 for verifier and
10 for integrator. Other roles receive no bonus. Every complete 30 seconds since
durable task registration adds one rank point, up to a common ceiling of 120.
Order eligible tasks first, then descending rank, ascending registration event
sequence, project ID and task ID. `ready_tasks()` uses the same order.

Registration age includes dependency waits, retry backoff and earlier attempt
execution. It is not continuous READY time. Negative calculated registration age clamps to zero; other clock rollback reduces the calculated age.
After at most 3,600 seconds of registration age, even a priority-0 worker reaches
the ceiling and earlier registration wins against newer arrivals. This is an
ordering property, conditional on actual admission and continued progress; it
does not promise a start deadline or bypass a quality dependency.

Task registration is validated against its bound event's sequence, type, project,
task and aware timestamp. Missing or mismatched history produces a bounded
`QUEUE_INCONSISTENT` error rather than removing a task from the population.

## Queue API

```python
Ledger.queue(project_id: str | None = None, limit: int = 100) -> dict
```

Validate IDs and an exact integer limit of 1–200; reject booleans. Unknown projects
raise `NOT_FOUND`. Use one SQLite read transaction and one sampled time. Return:

| Field | Contract |
| --- | --- |
| `schema_version`, `advisory` | `1`, `true` |
| `event_cursor`, `observed_at` | Global snapshot cursor and sampled epoch seconds |
| `policy` | Versioned ordering constants and age semantics |
| `capacity` | Global `used`, `limit`, `available`, `uncertain` |
| `entries` | At most `limit` registered-task summaries, eligible first |
| `total`, `truncated` | Complete selected task count and output truncation flag |

Each entry includes project/task/role/state, registration sequence, priority,
role bonus, base/effective rank, aging credit and registration age; `eligible`,
`dispatchable`, `blockers`, `blocker_count`, `blockers_truncated`; and the task's
`project_capacity` (`used`, `limit`, `available`). Raw objectives, definitions,
paths, resources, command arguments and arbitrary failure messages are omitted.

`eligible` checks project enablement, task status, cancellation, retry backoff and
current dependencies. `dispatchable` additionally evaluates global/project
capacity, resolved write scopes, exclusive leases and retry ceilings. It is an
individual preview against the snapshot, not a reservation or a jointly feasible
set. Two entries can both be dispatchable with one slot left, or conflict with
each other once either is admitted. Filesystem checks are sampled observations;
`reserve()` rechecks under its existing `BEGIN IMMEDIATE` transaction.

The shared admission function preserves reservation's first-fault behavior and
collects additional blockers only for diagnostics. Ineligible tasks report their
first eligibility blocker rather than speculative later blockers. Blocker kinds
are `eligibility`, `global_capacity`, `project_capacity`, `scope_changed`,
`scope_invalid`, `exclusive_resource`, `write_scope` and `retry_budget`.
Each has `code` and `kind`; conflicts add an `owner` containing attempt/project/task
IDs, and backoff adds `retry_after_seconds`. Deduplicate conflicts by owner/kind,
show at most 16, and retain the complete coalesced count and truncation flag.

Before materializing or ranking, enforce 10,000 selected tasks, 10,000 selected
projects, 20,000 domain-wide active attempts, and 20,000 domain-wide leases. Count
at most each budget plus one. `QUEUE_LIMIT` identifies the population, limit and
lower bound; no partial population is silently ranked. Filtering can reduce the
task/project population, while a lower output limit cannot. These diagnostic
budgets do not change scheduler admission limits. The database snapshot cannot
make unrelated filesystem mutations atomic.

## Metrics API

```python
runtime_metrics(ledger, project_id: str | None = None) -> dict
```

The authenticated CLI/MCP caller owns read authority. Run in one `query_only`
SQLite transaction, anchored by the global event cursor. Do not access worker
files, provider credentials or arbitrary command output. Limits are fixed:
100,000 selected events, 10,000 selected tasks, 20,000 selected attempts, and
16 MiB of needed terminal/quality/review event details. Row counters stop at each
budget plus one. Excess produces `METRICS_LIMIT`, never a silent partial report.

Return `schema_version=1`, `diagnostic_only=true`, project filter, sampled UTC
time, event cursor, history scope/limits/clock observations, global and selected
capacity, stored task/attempt populations, process outcomes, timing distributions,
slot usage, historical quality records, and unavailable measurements.

| Timing key under `seconds` | Measured population |
| --- | --- |
| `registration_to_first_reservation` | One sample per task ever reserved, including all pre-reservation waits |
| `reservation_to_release` | Confirmed released attempts, including preparation and exit observation |
| `first_start_ack_to_terminal_observation` | Started attempts with a confirmed terminal; use first acknowledgment only |
| `release_to_next_reservation_in_scope` | Each release with a later reservation in the selected scope |
| `active_reservation_age` | Still-charged reservations, including uncertain attempts, observed at snapshot time |

Distributions contain `samples`, `excluded_clock_reversals`, min/p50/p95/max/mean.
Use nearest-rank percentiles, six decimal places, and null values without samples.
Negative wall-clock intervals are excluded and counted; adjacent event timestamp
reversals are counted separately. Event order comes from sequence numbers.

Release gaps can include idle/dependency time; multiple releases can map to the
same next reservation. No physical-slot attribution or pure scheduler overhead
is claimed. Slot seconds sum selected reservation intervals, not CPU time.
Active intervals are right-censored and explicitly marked incomplete. Historical
gate/review counts include later-revoked records and grant no current acceptance.
Stored task-state counts are not freshly evaluated proof counts.

`retry_attempts` counts additional attempts beyond each task's first reservation;
`retried_tasks` counts distinct project/task pairs with more than one attempt.
Process outcomes distinguish success, failure, timeout, cancellation and failed
creation. Cancellation takes precedence, and a timed-out zero exit is not success.
Process success itself is not technical verification.

Cross-check registration and reservation event bindings, start evidence against
durable identity presence, and per-attempt terminal/release structure. Inconsistent
history raises `METRICS_INCONSISTENT`. `history.complete` describes complete
retained selected history within these bounds and checks; it is not a tamper-proof
audit or complete business-delivery evidence.

The `unavailable` map explicitly names continuous eligible wait, pure dispatch
latency, delivery-cohort wall time, technical first-pass rate, classified
integration-conflict rate, and complete CPU/memory/provider usage. Do not infer
these values from occupancy, process exits or historical passing records.

## Public surfaces and permissions

CLI: `queue [--project ID] [--limit N]`, `metrics [--project ID]`.
MCP: `parallel_queue` accepts only optional project ID and integer limit;
`parallel_metrics` accepts only an optional project ID. Both are read-only tools
available to existing read roles; readers see six tools and authorized planners
see seven including task registration. Neither surface reserves a worker,
changes a plan, runs a command, submits evidence or records final acceptance.

Hooks recognize these names only under the plugin's existing exact reserved MCP
namespaces. Returning an empty hook decision defers to existing host and server
authorization; it grants no additional permission. English policy and Skill
resources describe advisory semantics and preserve the single-controller queue.
