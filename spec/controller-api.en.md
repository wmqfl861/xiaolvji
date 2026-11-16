# Shared controller protocol — design version 1

> This is the retained architecture contract. Runtime 0.1 now implements a scoped local subset; see `docs/RUNTIME-CONTRACT.en.md` and the current runtime guide for actual entry points and capability limits. The following design statements are not installation or real-host acceptance claims.

This is the proposed interface of the plugin's runtime. These operation names are not existing Codex or ZCode tools. A future CLI or MCP server may expose them only after implementing authentication, persistence, host adapters, and execution boundaries. No scheduler service is included in this design package.

## Transport and authority

Use one controller per real resource domain. A local Unix-domain socket or Windows named pipe should have an explicit operating-system access policy. A TCP transport, if later added, requires authenticated encrypted connections and explicit origin/tenant handling. A random task ID or a producer field is not authentication.

The controller owns its database, launch outbox, cancellation endpoint, and final-review records outside worker-writable worktrees. Workers receive only the capability needed for their assigned attempt. A same-user unrestricted process can often access other same-user resources; a strict deployment must establish a real sandbox, distinct principal, or equivalent brokered boundary. Do not describe ordinary filesystem permissions under one unrestricted user as sufficient isolation.

Use distinct principals for planning, execution observation, technical verification, integration, and formal review. The execution gateway, not a worker report, attests that a process tree has stopped. The review authority, not an executor or verifier, records final acceptance. Approval covers an exact plan revision and scope digest; a field named `approved` supplied by a worker is not evidence of approval.

## Request envelope

Every mutating request carries `protocol_version`, `request_id`, `idempotency_key`, `project_id`, an expected state revision where applicable, and an operation-specific body. Resolve the authenticated principal from the transport. Do not trust a body-provided identity. A duplicate idempotency key with the same canonical body returns the recorded result; a different body with that key returns `IDEMPOTENCY_CONFLICT`.

Bound request size, string length, event rate, and replay retention. Store the request body digest and result in the same transaction as the state change. Use a controller-generated monotonic event sequence for ordering; remote wall-clock timestamps are descriptive, not an ordering authority.

## Operations

| Operation | Allowed caller | Required behavior |
| --- | --- | --- |
| `doctor` | Operator/coordinator | Report capabilities and evidence levels without granting permissions. |
| `project.enable` / `project.disable` | Authorized operator | Bind a project root, execution profile, and policy digest; disabling stops new admission. |
| `plan.register` | Authorized planning channel | Register the exact approved revision and scope evidence; invalidate affected old dependencies on revision changes. |
| `task.register` | Planner/coordinator | Validate schema, scope, graph, contracts, and ownership; create PLANNED, never directly ACCEPTED. |
| `task.ready` | Coordinator | Request eligibility recomputation; the controller determines READY from evidence and resource constraints. |
| `dispatch.request` | Coordinator/dispatcher | Atomically select or admit eligible work and reserve its budgets and resources. |
| `run.observe` | Bound host adapter/executor | Accept observed facts only for its assigned handle, attempt, and generation. |
| `run.cancel` | Authorized coordinator/operator | Stop admission for the selected scope and begin bounded cancellation; release only after confirmation. |
| `evidence.submit` | Worker/verifier | Store immutable evidence and candidate references; this does not attest process exit or final acceptance. |
| `integration.request` | Coordinator/integrator | Queue a candidate against an exact integration baseline through the sole authorized update path. |
| `review.record` | Independent review authority | Record approve/reject/rework for exact plan and delivery digests. Never expose this capability to a worker. |
| `status` / `events.tail` | Authorized readers | Return bounded, redacted views and a cursor, including blocked reasons and real capacity accounting. |

Errors have stable codes: `CAPACITY_FULL`, `DEPENDENCY_BLOCKED`, `SCOPE_CONFLICT`, `CONTRACT_STALE`, `REVISION_CONFLICT`, `UNAUTHORIZED`, `UNKNOWN_IDENTITY`, `CAPABILITY_UNSUPPORTED`, `INSTANCE_UNCERTAIN`, `CANCELLED`, and `IDEMPOTENCY_CONFLICT`. A no-capacity result is normal backpressure, not a reason to create an unmanaged worker.

## Admission transaction

The following is normative pseudocode, not a supplied executable:

```text
begin immediate transaction
  verify caller, enabled project, approved scope, and plan revision
  verify task graph, fixed baseline, and dependency evidence
  reject any active attempt for the task
  determine every applicable host, process, and resource budget
  check normalized path/resource conflicts against active leases
  reserve budgets and leases
  create a new attempt ID and increment its generation
  create one launch-outbox item with a stable idempotency key
  append the state event and recorded request result
commit
```

The executor consumes the outbox after commit. It must reconcile an existing launch with the same identity before creating another process. A database transaction cannot atomically include an operating-system launch. A lost launch acknowledgement is an uncertain instance, not a proven failure.

Count RESERVED, STARTING, RUNNING, STOPPING, and unresolved LOST instances against the applicable budget. An open native thread may remain counted even after a model turn stops. Do not release a host-thread reservation until its native closing fact is confirmed. Use separate counters for active workflow asks and provider requests; neither substitutes for the managed-worker budget.

## Event acceptance

`schemas/event.schema.json` defines the envelope. Its `payload` is deliberately event-specific; the receiving handler must additionally validate its versioned payload contract and authenticated producer.

| Event family | Additional required facts |
| --- | --- |
| Run reserved/start acknowledged | Task, attempt, generation, allocated resources, actual native handle or executor launch identity. |
| Run reported | Candidate digest and evidence references; explicitly not a terminal occupancy fact. |
| Run exit/cancel confirmed | Authorized observer, actual handle identity, exit/termination facts, remaining descendant status, and time observed. |
| Run lost | Last observation and uncertainty reason; no automatic release. |
| Technical verification | Exact candidate and contract digests, check identities, environment digest, outcomes, and verifier identity. |
| Integration verified | Exact previous integration baseline, candidate set, resulting tree/commit, and checks on that combination. |
| Review recorded | Exact plan and integrated delivery digests, decision, designated reviewer, and authenticated review record. |

Check the transition against the current attempt generation and controller revision. Record stale observations for diagnosis without changing a newer attempt. A repeated terminal event must never return another slot. A new resume is a new admission even when the host reuses the native agent ID.

## Rolling dispatcher

Persist the task graph and reverse dependency index. On each accepted material event, recompute only affected eligibility, then choose a compatible set of READY tasks using critical-path priority, validation backlog, age, and resource fit. Acquire the actual reservations transactionally; a previously computed selection is only a proposal.

Start admitted tasks until a relevant budget is full or no eligible compatible task remains. Do not await the longest sibling before processing another result. Trigger planning at meaningful scope/contract milestones to discover additional work inside the approved plan. Do not trigger unlimited model planning after every tool event.

If a host's verified native workflow already owns the rolling queue, do not run a second independent scheduler over the same slots. Assign one owner of each capacity domain, correlate its events, and state which global protections remain unavailable in cooperative mode.

## Evidence and integration

Evidence must identify source baseline, produced tree/commit, contract versions, exact command argv, working directory, environment summary, result, exit status, timestamps, and bounded log/artifact references. `skipped` and `not_run` are distinct from `passed`. A source, contract, dependency, or environment change invalidates the relevant evidence according to declared applicability.

An integration candidate contains the expected integration-ref value. Update that ref only through the integrator after verifying the actual candidate combination. If the baseline changed, recompute and reverify affected checks before a compare-and-swap update. A clean merge proves only that a textual operation succeeded.

The local development agent's self-tests and an internal verifier can establish technical readiness. They do not replace the independent review authority specified by the user's workflow. Wait for that authority when required; do not use Stop continuation to fabricate approval.

## Recovery and shutdown

Persist reservations, outbox entries, observed handles, process identity including start-time information, and generation ownership. On restart, reconcile every nonterminal instance before launching replacements. A heartbeat expiry does not terminate a process. Before reassigning an exclusive resource, confirm termination or enforce a real revocation boundary that rejects old-generation access.

On cancellation, prevent new admission first; request shutdown; wait a bounded grace period; terminate the owned process tree where authorized; confirm; then release. Retain evidence and unmerged candidates. Never kill a process solely by a stale PID. An uncertain stop remains visible and consumes the relevant safety reservation.

The default design allows two automatic retries after the first attempt. All retries remain within task, elapsed-time, cost, and permission budgets. Do not retry unknown irreversible side effects. If no runnable work remains, report the blocker and stop/pause without busy polling.
