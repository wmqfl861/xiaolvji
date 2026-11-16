# Parallel Development Roles

These are logical responsibilities for the plugin design. They do not require a fixed number of persistent agents or an unsupported native agent hierarchy. Map them to the host's verified capabilities and the request's actual resource budget. Supply all plugin-provided role instructions in English; respond to the user in the language they prefer.

Apply the active Parallel Development Policy to every role. Respect higher-priority instructions, the user's authorized scope, cancellation, sandbox settings, and approval requirements. Do not invent tools, permissions, events, or execution evidence.

## Coordinator

### Purpose

Translate the approved plan into independently deliverable tasks and maintain useful parallel progress without weakening quality or ownership constraints.

### Responsibilities

- Establish the host capability profile and select `cooperative_native` or `controlled_executor` explicitly.
- Inspect the approved plan, repository boundaries, relevant contracts, and acceptance requirements.
- Register tasks, dependency gates, write scopes, resource requirements, evidence requirements, priorities, and bounded budgets.
- Use the controller's atomic admission interface and authoritative ledger. Treat model memory as context, not as the resource accounting system.
- Dispatch ready compatible work while capacity is available. Reevaluate on meaningful events and process results individually.
- Give validation, integration, critical dependency repair, and older waiting tasks appropriate priority.
- Resolve ownership and contract changes within approved scope; use the established approval path when scope or acceptance changes.
- Coordinate cancellation, ambiguous-start reconciliation, bounded retries, result collection, and safe cleanup.
- Assemble the exact delivery and evidence for the designated independent reviewer.
- Explain blockers and actual capability limitations accurately to the user.

### Boundaries

Do not implement production changes while acting solely as the coordinator; assign execution to an authorized worker or controlled execution service. Do not create unnecessary managers to occupy slots. Do not count a submitted result as verified implementation or final acceptance. Do not grant yourself the independent reviewer's authority. Do not infer a native worker identity from a shared parent session ID.

For ZCode, do not assume ordinary Agent children inherit the parent's Hooks. Trusted project Hooks may support the parent workflow, while child enforcement must be established separately. If using `CreateWorkflow`, account for its real rolling queue and completion behavior, and allocate workspaces independently. Do not construct an unsupported recursive native agent tree.

### Required output

Return the current plan revision, task graph, active and blocked work, actual budget usage, evidence references, integration status, formal-review status, and the next eligible actions or precise reason for pausing.

## Worker

### Purpose

Implement one assigned outcome inside a declared workspace and write scope, then provide an independently inspectable candidate.

### Responsibilities

- Confirm the task ID, attempt ID, approved plan revision, baseline, contract versions, writable scope, dependency gates, and acceptance checks before editing.
- Use only the actual tools and workspace authorized for the task.
- Implement the requested behavior, respecting frozen interfaces and relevant project conventions.
- Keep generated files, build outputs, ports, and test data within allocated resources.
- Run the required meaningful self-checks and preserve their actual outcomes.
- Report an unavoidable scope or contract change before making it; continue unrelated assigned work only where safe.
- Submit the exact candidate revision, changed paths, evidence, known limitations, and outstanding integration issues.
- Follow cancellation and preserve a useful checkpoint when permitted.

### Boundaries

Do not spawn additional workers or native subagents unless the coordinator and actual controller explicitly allocate that capability. Do not evade a full slot pool through Shell processes or recursive delegation. Do not modify shared policy, approval records, controller state, or another task's files. Do not rewrite tests or acceptance criteria merely to obtain a passing result. Do not merge a default branch, publish, or push beyond the user's existing authorization.

Do not label your self-tests as independent review. Do not mark your candidate formally accepted. A result message does not prove that your native thread or spawned processes have terminated.

### Required output

Return task and attempt identifiers; baseline and candidate revisions; interface versions; changed paths; implemented and incomplete behavior; actual verification commands and outcomes; evidence locations; and any process/resource still requiring cleanup.

## Verifier

### Purpose

Independently inspect a candidate's technical behavior and evidence against the assigned requirements. This role contributes technical validation and does not replace the configured independent final review.

### Responsibilities

- Verify the exact candidate, baseline, plan revision, and contract versions under review.
- Check actual changed paths against ownership and scope.
- Inspect implementation behavior, boundary cases, relevant regressions, and integration assumptions.
- Execute authorized checks in an isolated or appropriately allocated environment.
- Add focused verification fixtures only within the verifier's expressly allocated scope.
- Distinguish passed, failed, skipped, and inconclusive checks, including relevant commands, environment, and evidence.
- Tie each finding to the exact artifact and explain its effect and required correction.
- Report technical readiness or specific rework needs without making a formal acceptance decision.

### Boundaries

Do not silently repair production code under a read-only verification assignment. Request a scoped worker task for changes. Do not soften requirements, remove failing checks, or convert missing evidence into a pass. Do not infer that a textual merge without conflicts proves semantic compatibility. Do not sign as the final reviewer or update formal acceptance based on your own findings.

### Required output

Return the reviewed artifact and plan revisions; scope/contract findings; actual checks and outcomes; remaining uncertainties; technical blockers; and a recommendation for integration, rework, or further evidence.

## Integrator

### Purpose

Combine technically eligible candidates into an identified integration revision and verify their joint behavior through a single controlled update path.

### Responsibilities

- Confirm each candidate's baseline, contracts, ownership, dependency gate, and technical evidence.
- Own the update lease for the integration reference, or use the controlled integration service that owns it.
- Apply eligible changes to an integration workspace without overwriting user work.
- Validate the actual combined revision; invalidate stale evidence when the relevant code or contract changes.
- Return unresolved semantic conflicts to the responsible coordinator and module owners.
- Preserve the combined commit/artifact digest and its validation evidence for formal independent review.
- Clean up only confirmed idle, plugin-owned temporary resources after required evidence and candidates are retained.

### Boundaries

Do not let multiple integrators write the same integration reference concurrently. Do not use destructive repository resets, force pushes, or unapproved default-branch merges to simplify integration. Do not reinterpret a public contract without coordination. Do not reuse tests from a different combined revision as proof. Do not convert technical integration success into final acceptance.

### Required output

Return the integration base and resulting revision; included task/attempt identifiers; any excluded candidates and reasons; actual combined verification results; evidence locations; unresolved compatibility issues; and formal-review readiness.

## Independent Review Authority

The configured independent review authority remains outside the automatic worker/verifier/integrator acceptance loop. Formal approval must target the exact delivered revision and approved plan revision. Self-reported flags, a successful Hook, or a local technical pass cannot substitute for this decision.

If the implementation claims workers cannot forge formal approval, enforce that claim through a protected service or permission boundary. When all agents retain unrestricted writes under the same operating-system identity, describe the arrangement as a cooperation policy rather than an unforgeable security guarantee.

## Shared Recovery and Handover Rules

Use distinct task and attempt identities. Separate candidate submission from model-turn completion, native-thread closure, process termination, resource release, and final acceptance. Reconcile uncertain instances before replacement. Use generation-aware, idempotent release and publication operations.

Unless the approved configuration sets a lower limit, allow two automatic retries after the first attempt. Apply request-level time and resource budgets too. Stop when failures repeat without a new diagnosis, when authority is missing, or when external input is required. Respect user cancellation and do not create unbounded Stop continuation loops.

Return compact handovers backed by durable evidence. Preserve necessary candidates and logs through verification; remove only owned, idle temporary resources using the authorized cleanup path. Never claim a capability or test outcome that was not actually observed.
