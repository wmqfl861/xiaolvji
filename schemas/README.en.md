# Versioned design schemas

These JSON Schema Draft 2020-12 files describe plugin-owned data, not native Codex or ZCode tool arguments.

| Schema | Purpose | Example |
| --- | --- | --- |
| `task.schema.json` | Task contracts, dependency gates, ownership, acceptance, and retries | `../examples/task.json` |
| `event.schema.json` | Normalized controller event envelope | `../examples/event.json` |
| `capabilities.schema.json` | Evidence-based host capability report and selected profile | `../examples/capabilities.json` |

All examples are synthetic. Commit/digest values do not identify actual project work, and example event fields do not constitute execution or approval evidence. `project-config.json` is a proposed plugin configuration with `enabled: false`; it is not a native host configuration or an instruction to activate a plugin.

## Required semantic validation

Schema validation is necessary but cannot replace these runtime checks:

1. Authenticate the caller and resolve its authorized project, plan, and role.
2. Validate the plan revision and approved scope digest through the designated authority.
3. Reject cycles, missing dependency producers, self-dependencies, and incompatible gate/contract references.
4. Resolve project-relative paths against the actual workspace and normalize symlinks, case, separators, volumes, and directory ancestry before computing conflicts.
5. Bind baselines and evidence to real immutable artifacts; reject stale, missing, or mismatched digests.
6. Allocate budgets, task ownership, and exclusive resources atomically.
7. Validate event-specific payloads, authenticated producer permissions, generations, and legal state transitions.
8. Preserve separate facts for model completion, thread closure, process-tree exit, technical verification, and formal acceptance.
9. Derive the effective execution profile from required capabilities. A client-supplied `integration_tested` label is not sufficient proof.
10. Check the argv, working directory, environment, permissions, and resource cost of each requested validation command before execution.

## Critical event payload contracts

The event envelope rejects unknown top-level properties. The following payloads and their nested records also reject unknown properties; extending them requires an explicit protocol change. An enclosing `attempt_id` always requires both `task_id` and `generation`, and an enclosing `generation` always requires both identity fields.

| Event | Required binding and payload |
| --- | --- |
| `resource_released` | Enclosing task, attempt, and generation; exact `lease_id`, `resource_id`, `resource_kind`, `release_basis`, and nonempty immutable `evidence_refs`. |
| `evidence_submitted`, `technical_verification_passed` | Enclosing task, attempt, and generation; `plan` containing `id`, `revision`, and `scope_digest`; exact `artifact_digest`; nonempty `evidence_refs`. |
| `integration_verified` | Exact `plan`, `integration_ref`, `integration_baseline`, `integration_result`, nonempty `candidates`, and nonempty `evidence_refs`. Each revision binds a Git commit and artifact digest; each candidate binds task, attempt, generation, and artifact digest. |
| `review_recorded` | Exact `plan`, `delivery_digest`, `decision`, `review_record_id`, and `review_authority` identity. Decisions are `approve`, `reject`, or `rework`; optional `evidence_refs`, when supplied, must be nonempty. |
| `run_exit_confirmed` | Enclosing task, attempt, and generation; `process_tree_terminated`, an `exit_fact` with stable instance identity and observation time, and nonempty `evidence_refs`. |

Integration and review may describe a project or a task without an enclosing attempt. Candidate attempts remain explicit inside an integration payload. The controller must resolve them to the approved plan and reject duplicate or incompatible candidate ownership; array uniqueness alone does not enforce that rule.

Every evidence reference contains an evidence-store `id` and its content `digest`. The referenced record must exist, be immutable, originate from an authorized observer where required, and cover the exact event target. A technical-pass reference must contain the required check identities, contract bindings, environment, and outcomes. Schema validity cannot turn submitted evidence into a technical pass or establish the authenticity of a final review. In particular, `producer` and `review_authority` fields are claims for correlation; authenticate the actual caller and validate the review record through the designated independent authority.

For a process-tree exit, `exit_fact.kind` is `process_tree_exit`, an actual exit code or terminating signal is required, and `process_tree_terminated` must be `true`. For a confirmed native thread closure, the kind is `native_thread_closed` and `process_tree_terminated` must be `null`: the host closure supplies no process-tree termination fact. A stopped model turn is neither kind. Remaining descendants or an unknown exit must remain nonterminal and must not be represented by a false terminal claim.

Release evidence must be appropriate for the exact resource. A lease expiry is not a release basis. `effective_write_revocation` is structurally restricted to an `exclusive_write_scope`; it cannot free a still-running process or an open native-thread slot. The controller must also verify every other basis against the actual execution profile and reservation, reject old-generation releases, and apply an idempotent state transition atomically. A valid event can still be stale, unauthorized, or false.

The remaining event types retain open payload objects in this design schema. Their receiving handlers must implement separate versioned contracts before those events may cause a state change. Handler semantics and transport authorization are described in `../spec/controller-api.en.md`; the document is a design contract, not implemented validation code. Parsing JSON or checking these structures does not verify host compatibility, authenticated facts, or concurrency invariants. Use a Draft 2020-12 validator with explicit format validation during implementation; the `format` keyword alone does not guarantee date-time validation in every validator configuration.

## Retry policy

The task schema permits zero, one, or two automatic retries after the initial attempt, consistent with the global limit of two. The effective limit is the stricter of the task and project policy; elapsed-time, cost, permission, and side-effect constraints still apply. Retry eligibility and generation changes are runtime decisions, not consequences of schema validity.

Protocol changes require explicit schema and adapter versioning. Do not let individual adapters silently fork task or lifecycle semantics.
