---
name: parallel-development
description: Organize approved software development into independently deliverable tasks with explicit dependencies, isolated writes, rolling parallel dispatch, and unchanged quality gates. Use for multi-step implementation, repairs, migrations, integration, or validation when separate modules or investigations may proceed concurrently, and when an active development plan needs rescheduling after progress, failure, or changed dependencies.
---

# Parallel Development

<!-- Distributed by the runtime plugin builder. Loading the Skill does not enroll a project, grant hook trust, or install a personal skill. -->

## Use the implemented runtime

Use the actual `parallel-dev` launcher installed with this plugin. Run `doctor` before relying on a host capability. Runtime 0.5 supports a controlled executor that is offline by default, scoped provider transport, explicit native deployment acceptance, and continuous queue watching. A native protocol fixture is not a real-provider or managed-policy acceptance. Require a current controller-owned `host verify` record for the exact standalone deployment, model, quality, binary, runtime, and project instructions. Existing unsupported management policies block; never ignore them or change profiles to make a task run.

For controlled offline work, grant independent directory scopes or existing exact files. Missing exact files are blocked; never widen them to their parent without approved scope. Exact file mounts permit in-place edits, not atomic replacement or deletion of the mount point. Keep Git metadata read-only. Put generated check outputs in private temporary directories; strict verification source is read-only. A trusted controller service collects reports outside each check sandbox. Neither a child-written success message nor an unbound report establishes a quality gate.

Use the enrolled controller's state directory consistently. `status --project PROJECT`, `queue --project PROJECT --limit 100`, `metrics --project PROJECT`, and `events --after CURSOR` are read operations. Register tasks with `task register PROJECT --file TASK_JSON` or the authenticated `parallel_register_task` MCP tool when exposed. The task schema is bundled at `src/xiaolvji/resources/task.schema.json` relative to the plugin root. A reader-only MCP connection cannot register tasks. Do not obtain a stronger capability from a worker or invent an unavailable tool.

Use `parallel_queue` to inspect current admission blockers and `parallel_metrics` to inspect bounded historical observations. Queue results are advisory: `eligible` does not include capacity or conflicts, and each `dispatchable` entry is evaluated individually against the current snapshot. Only atomic reservation assigns a slot. Honor truncation and population-limit errors. Registration age includes dependency and backoff waits; metrics do not prove speedup, continuous READY wait, or current quality acceptance.

Use `parallel_project_tree` and `parallel_read_file` to inspect committed source through the coordinator interface. Keep the returned exact commit pinned across pages and reads. These tools do not expose untracked files or permit shell execution. The Hook may defer this narrow interface to the host and MCP authorization without treating the coordinator as an authenticated worker. Do not imitate a tool name to evade the Hook.

Start the managed queue with `run PROJECT`, or `run --all` for explicitly enrolled projects sharing one execution profile and resource domain. For controlled native workers, always supply `--provider-config /absolute/providers.json` matching the accepted deployment; the run command does not reconstruct provider configuration from the acceptance record. Use `--watch` when the authorized session must keep accepting newly registered eligible work across idle periods; keep an explicit timeout. The controller persists admission, starts private workers, replenishes capacity, schedules checks, and integrates verified candidates. While it runs, keep looking for useful independent work inside the exact approved plan and register suitable tasks. A full capacity pool is normal backpressure; never start a second native queue or an unmanaged worker to bypass it.

Use `cancel PROJECT --task TASK` or `cancel PROJECT` when cancellation is authorized. Inspect retained uncertain instances before retrying. If the controller has paused or exited while waiting for external review, run it again after the actual blocker is resolved; a Hook cannot wake an exited controller automatically.

Prepare the exact delivery for the configured reviewer. The `review` command requires that reviewer's capability and an exact delivery digest plus review record. Never read, request, copy or use reviewer credentials while acting as a worker, planner, verifier, or integrator. Passing checks or holding a controller slot does not grant review authority.


## Establish authority and actual capabilities

Read the user's current request, the approved development plan, the effective project instructions, and the adapter capability record. Apply the active Parallel Development Policy. Keep plugin-provided instructions in English while communicating with the user in their preferred language.

Treat this Skill as a workflow over real tools. It does not create tools, privileges, lifecycle events, or a background service. Before relying on a capability, distinguish documented support, locally detected support, and behavior verified on the installed host version. If a required capability is missing, block that execution path and continue only preparation that does not depend on it. Do not downgrade the selected profile unless the user has already explicitly authorized that fallback or explicitly chooses it. State the limitation and never simulate successful tool calls in prose.

Follow the project's configured division of responsibility: the local development agent implements, self-tests, and delivers within the approved plan; the designated independent reviewer makes the formal review decision. Additional technical checking does not transfer that authority.

## Select the operating profile

Choose and record one profile for the request:

- `cooperative_native`: use controller-owned CLI workers and the shared ledger with the explicitly accepted same-user trust boundary. Treat unobserved operations and ambiguous lifecycle states as coverage gaps. Do not claim OS isolation or start a second native agent queue.
- `controlled_executor`: route worker startup, writes, cancellation, result publication, and resource accounting through the controller-owned execution gateway. Require verified workspace/process isolation and the configured permission boundary. Refuse to label this profile strict if workers can bypass the gateway or mutate protected approval state.

For the reviewed ZCode integration, trusted project Hooks are supported, but ordinary Agent children do not inherit parent Hooks. Upstream native `CreateWorkflow` provides a rolling queue without worktree isolation, but this runtime has no integration for that queue or its capacity. Use only the controller-owned queue for enrolled work. Supply role instructions and task context explicitly to the admitted CLI workers. Do not start recursive native children or native workflows alongside them.

For Codex, use only the installed version's supported tool and Hook schemas. A shared parent `session_id` is insufficient to identify an individual worker. Do not assume the child's working directory or sandbox differs from its parent's. Bind task, attempt, native handle, and workspace using supported, observed information. Do not add invented permit fields to a native tool call.

## Preserve the scoped provider boundary

Keep ordinary controlled commands and all verification/integration checks offline. Enable provider transport only through an explicit controller configuration and a currently admitted attempt. Keep upstream credentials outside the sandbox; do not search or import existing accounts, host login state, ambient credentials, or model settings. Use the controller-issued unnamed socketpair for that attempt and its bridge inside the private loopback namespace. Never replace it with host networking, a shared proxy, or an ambient service socket.

Require an immutable owner-supplied model and quality snapshot. Use matching broker constraints and sealed, read-only configuration in private HOME. Preserve the exact selected model and quality settings; reject unsupported combinations rather than substituting defaults or weakening quality. Use Codex's external-sandbox adapter only for the explicitly verified standalone deployment inside the attested outer boundary. It does not preserve every ordinary native approval semantic; unsupported existing policies block. Never auto-approve native requests, change trust, or discard applicable rules. Keep readiness separate from configuration validation and synthetic transport evidence.

Require a complete native result in addition to successful process-tree exit. Reject native errors, metadata fallback, cancellation, incomplete or truncated logs, and inconsistent thread/turn identities. ZCode exit zero or an idle projection alone is insufficient. Let the controller recheck durable executor evidence during recovery before submitting a candidate.

Respect the broker's request-count, request/response-byte, deadline, and local in-flight limits as well as the ledger's development capacity. Budget rejection does not authorize an unmanaged retry or another channel. Revoke an attempt's grant on cancellation or release and reconcile its process state separately. A closed channel or revoked capability establishes neither worker tree termination nor termination of remote provider processing. Never use either event alone to free a development slot.

## Create the task graph

Identify separable outcomes within the approved scope. Prefer tasks with stable inputs, a clear owner, and a meaningful independently verifiable result. Avoid splitting tiny dependent edits merely to increase agent count.

For every task, register:

1. A unique task identifier and approved plan revision.
2. The objective, expected deliverable, and exclusions.
3. A fixed source baseline and relevant interface/contract versions.
4. Dependencies with explicit release gates and artifact references.
5. Allowed write scopes and shared resources requiring ownership.
6. An execution workspace and any build, port, database, or output isolation requirements.
7. Acceptance criteria, verification requirements, and evidence to return.
8. The assigned logical role, scheduling priority, and retry/time budgets.

Check the graph for cycles and missing producers. Do not silently satisfy a dependency with an unverified claim. Distinguish these gates:

| Gate | Required evidence |
| --- | --- |
| `contract_ready` | A versioned interface and behavior contract is available; work with a test double is explicitly allowed. |
| `implementation_verified` | The specified implementation artifact passed its required technical checks. |
| `integrated_verified` | The actual combined revision passed the required integration checks. |
| `final_accepted` | The authorized independent reviewer accepted the exact delivery and plan revision. |

Allow parallel implementation behind a frozen contract when appropriate, but retain later real integration checks. Consider semantic overlap: protocol units, errors, transaction behavior, data ownership, and common configuration can conflict even when paths differ.

## Admit work through the controller

Use the authoritative shared ledger for plugin-managed work. Request admission through its real interface; never replace it with a conversational count.

Require one atomic admission operation to verify eligibility, reject a duplicate active attempt, check all applicable budgets, and reserve capacity plus exclusive resources. Respect the host's real accounting for open native threads and the executor's separate accounting for processes, model requests, builds, and tests.

Start the task only after admission succeeds. Correlate its task ID, attempt ID, native handle, and workspace without inventing host arguments. On a proven startup failure, release its unconsumed reservations. If startup may have succeeded but its response was lost, reconcile the actual instance before retrying.

Keep all actual descendants within the applicable budget. Do not fill the host's slots with coordinating agents that then cannot start their workers. Logical roles do not require a persistent native agent each.

## Run a rolling scheduling loop

After initial planning and after each meaningful completion, failure, cancellation confirmation, dependency change, or validation result:

1. Reconcile observed host/executor state with the ledger.
2. Record new evidence without treating a completion message as final acceptance.
3. Refresh affected dependencies and conflict compatibility.
4. Select ready work that fits current capacity and resources.
5. Admit and start compatible work until capacity or eligible work is exhausted.

Process results individually. Do not wait for an entire batch merely because it started together. Use the controller-owned queue as the single scheduler for enrolled work. Native workflow integration is not implemented in this runtime; do not create a competing queue or infer ledger authority from its events.

Assign priority to tasks that unlock critical dependencies, fix verified blockers, or reduce validation/integration backlog. Respect the controller's bounded priority aging: priority plus a verifier/integrator preference receives one rank point per 30 seconds since registration, up to a shared rank of 120, then registration order breaks ties. This prevents indefinite overtaking by newer high-priority tasks when admission is possible; it does not override dependencies, conflicts, quality gates, or capacity, and it promises no start deadline. Avoid repeated whole-project replanning after trivial progress. Idle capacity is acceptable when no useful task meets the admission requirements.

Do not lower model quality, skip verification, or invent additional product work to improve utilization statistics.

## Execute within ownership boundaries

Provide each writer with a unique workspace and a stable baseline. Assign separate build outputs and mutable test resources. Give shared interfaces, root build configuration, dependency lock files, migrations, and integration references explicit owners.

When a task needs an ownership or contract change, report the specific change and affected consumers to the coordinator. Continue unaffected authorized work where possible. The coordinator may reorganize work within the approved plan; changes to scope or acceptance require the established approval path.

Treat Hook checks as one enforcement layer. Where strict guarantees are required, rely on the controlled executor's actual write/permission boundary. A policy statement or clean worktree directory does not prove that another write path is blocked.

## Collect candidates and verify integration

Require each handover to include:

- Task and attempt identifiers, source baseline, contract versions, and delivered commit or artifact digest.
- Actual changed paths and any requested scope deviations.
- Implemented behavior, incomplete work, and known limitations.
- Commands/checks actually performed, relevant environment, outcomes, and evidence locations.
- Dependencies or integration decisions requiring attention.

Distinguish `passed`, `failed`, and `skipped`. Never reuse an old test report as proof for a changed artifact. Choose verification appropriate to the change and retain all required existing quality gates. Do not weaken tests to make a candidate pass.

Let the integrator update a shared integration reference through one controlled entry. Verify the current integration baseline combined with the candidate. Parallel verification is acceptable on immutable, identified snapshots. Return ambiguous semantic conflicts to the appropriate owner rather than guessing a new public contract.

Record technical readiness separately from formal review. Prepare the exact combined delivery and evidence for the designated independent reviewer. A worker, verifier, or integrator cannot approve its own work as the final review authority.

## Release resources safely

Keep result submission, model-turn stopping, native-thread closure, process termination, and formal acceptance separate. Use the host/executor's actual terminal-state evidence to release capacity. Before reassigning an exclusive mutable resource, confirm that the old writer has stopped or has lost effective permission to write or publish.

Use attempt generations and fencing identifiers. Make release operations idempotent. Ignore stale completions for a newer attempt. A missed heartbeat is uncertainty, not permission to run a conflicting replacement. Preserve candidates and evidence through verification and authorized cleanup.

## Recover within bounded budgets

Unless the approved configuration is stricter, permit two automatic retries after the initial attempt, for three attempts total. Apply the request's elapsed-time and resource budgets as additional limits. Retry only when the failure is diagnosed as transient or a bounded repair remains within scope. Reconcile unknown side effects before retrying; do not repeat an irreversible action blindly.

Stop new admission after cancellation. Do not use a Hook to override the user's interruption. If all remaining work requires external input, review, unavailable resources, or a changed requirement, report the specific blocker and pause. Do not make a Stop Hook continue indefinitely because the task table is nonempty.

## Report the outcome

Report completed deliverables, unresolved blockers, actual validation results, formal-review status, and known capability gaps. State whether the run used cooperative native orchestration or a verified controlled executor. Distinguish fixture checks from real-host tests. Never claim zero defects, complete isolation, automatic background persistence, or support for an untested host profile.
