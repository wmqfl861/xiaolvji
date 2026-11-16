# Runtime implementation contract — 0.5.0

Updated 2026-11-16 (UTC). This file preserves the shared Python interfaces established in 0.1.0 and the strict execution boundary from 0.2.0, and adds the scoped provider interfaces below. Python 3.11+, standard library only; SQLite is the single resource-domain authority. Existing JSON task schema version 1 remains normative. Interfaces use JSON-compatible dictionaries so adapters do not fork public schemas. Runtime errors are `xiaolvji.contracts.Fault(code, message, details=None)`.

Version 0.5.0 adds bounded priority aging, shared admission diagnostics, history metrics and parser/recovery fixes. Version 0.4.0 added policy-bound native Codex acceptance, complete native result verification, continuous queue watching, and committed-source coordinator tools to the existing per-attempt provider channel and Linux controlled execution. Default commands and all source checks stay offline. It does not enable unverified deployments, cgroup resource quotas, Windows/macOS execution, or automatic final review. The OS trust model is in [STRICT-SANDBOX-CONTRACT.en.md](STRICT-SANDBOX-CONTRACT.en.md); provider semantics are in [PROVIDER-TRANSPORT.en.md](PROVIDER-TRANSPORT.en.md). Release-wide results belong to [VALIDATION.json](../VALIDATION.json) and [validation/](../validation/).

## Shared utilities (root owner)

`contracts.py` exports `Fault`, `canonical_json(value) -> str`, `digest(value) -> str` (sha256 over canonical JSON), `validate_task(task) -> dict` (raises Fault; returns normalized deep copy), `normalize_scope(root, scopes) -> list[dict]`, `scopes_overlap(left, right) -> bool`, `path_in_scope(path, scopes) -> bool`, `utc_now() -> str`.

Normalized scope entries have `path` (relative POSIX path), `kind` (`file`/`directory`), `canonical` (absolute real path, normcase), `key` (casefolded conservative absolute key). Reject traversal, symlink components, glob syntax, .git / .parallel-dev and out-of-root paths. Empty write scopes are valid for readers. `Principal` is a trusted internal dataclass (`id`, `role`); external callers are authenticated by the controller before constructing it. Role strings never authenticate network/JSON callers.

## Ledger (store.py owner)

`Ledger(path: Path|str, global_limit: int|None=None)` opens a per-call SQLite connection; transactions BEGIN IMMEDIATE. First initialization defaults to 4, subsequent opens must not silently change the persisted limit. No processes are started inside transactions.

Methods (all JSON dictionaries unless stated):

- `enable_project(project_id, root, config, principal, request_id)`; config has `project_limit`, `profile` (`controlled_executor` or explicit `cooperative_native`), `review_authority` (principal id). Validate root is outside DB directory and vice versa. Preserve existing config unless idempotent; changes must not invalidate active runs silently.
- `disable_project(project_id, principal, request_id)` stops admissions, not running processes.
- `register_plan(project_id, plan, principal, request_id)`; plan has `id`, `revision`, `scope_digest`, `approved_scope` (scope list), `approval_ref` (operator supplied concrete approval record reference), optional `contracts` (list of id/version/digest). A replacement revision must not leave older tasks/evidence eligible.
- `register_task(project_id, task, principal, request_id)` validates full schema, plan/scope/contracts, DAG, immutable task identity. Forward references allowed but block eligibility; cycles rejected as graph fills.
- `ready_tasks(project_id=None) -> list[dict]`: each item `{project_id, task_id, task, state, created_seq}`; eligibility recomputed from gates, retry delay, current plan; deterministic priority then age (verifier backlog preference).
- `reserve(project_id, task_id, principal, request_id) -> attempt`: atomic caps across projects, task uniqueness, normalized path and global exclusive resource conflicts, attempt generation, leases, durable outbox. Counts RESERVED/STARTING/RUNNING/STOPPING/LOST. Return `{attempt_id, generation, project_id, task_id, state, task, project, launch_key}`.
- `claim_launch(attempt_id, generation, principal, request_id) -> attempt` transitions RESERVED->STARTING once; repeated claims with distinct request ids must not relaunch. `launches() -> list[attempt]` contains RESERVED only.
- `observe(attempt_id, generation, kind, payload, principal, request_id)`; kind `started` (identity dict), `lost` (reason), `exited` (exit_code, process_tree_terminated:true, identity, optional Boolean timed_out/cancelled), `start_failed` (reason, no_process_created:true). Identity must match current launch; stale generation rejects. Confirmed exit releases capacity/leases exactly once. Exit 0 without timeout or cancellation -> SUBMITTED; failure -> READY if retries remain or BLOCKED (with backoff); cancellation -> CANCELLED. Unknown/expired leases never release. The terminal observation is durable and exposed as `attempt.outcome` for recovery.
- `cancel(project_id, task_id|None, principal, request_id)` prevents future admissions then marks active instances STOPPING; queue tasks CANCELLED. Returns affected attempts for gateway cancellation.
- `record_gate(project_id, task_id, gate, artifact_digest, binding, principal, request_id)`; gate contract_ready planner, implementation_verified verifier, integrated_verified integrator, final_accepted reviewer. Binding must match current task plan, current candidate artifact if applicable, and review authority. Final acceptance requires integrated_verified on exact digest. Evidence/review authority is not a worker.
- `submit_candidate(attempt_id, generation, candidate, principal, request_id)` requires confirmed successful exit and current attempt. Candidate includes `commit`, `artifact_digest`, `plan` and optional changed_paths. Repeated different candidates invalidate applicable gates, never retain stale approval.
- `project(project_id)`, `task(project_id, task_id)`, `attempt(attempt_id)`, `active_attempts(project_id=None)`, `status(project_id=None)`, `events(after=0, limit=1000)` read methods.

Every mutation stores idempotency key + canonical body digest + result atomically. Duplicate identical request returns original result; changed payload conflicts. Caller's identity/role included in replay digest. Expected revision is enforced through exact plan and attempt generation rather than caller timestamps. Durable events have integer `seq`. Task state and resource occupancy are separate.

## Workspaces and process executor (executor/workspace owner)

`workspace.py` exports `Workspaces(root)` with `create(repository, attempt_id, base_commit) -> dict` containing path, repository, base_commit, branch; `snapshot(workspace, scopes) -> dict` producing commit/artifact_digest/changed_paths after checking every write incl rename/delete, symlink escapes, submodules; `remove(workspace)` only explicitly owned workspace. Prefer separate local clones / worktrees with private Git metadata to prevent workers mutating shared refs. Explain choice. Root can use helper `git(*args,cwd=...)` if exported; no shell.

`executor.py` exports `ProcessExecutor(state_dir, profile='cooperative_native')`, `probe() -> dict`, `async run(attempt:dict, argv:list[str], cwd:str, env:dict|None=None, stdin_text:str|None=None, timeout:float=3600, on_started:async callback|None=None, sandbox_policy:dict|None=None, provider_runtime:dict|None=None) -> dict`, `async cancel(attempt_id, grace=2.0) -> dict`, `reconcile(identity:dict) -> dict`.

Run returns `exit_code`, `identity` (PID plus start identity/session identity), `process_tree_terminated` bool, `stdout_path`, `stderr_path`, `cancelled`, `timed_out`. The strict path also records `worker_started`, actual `sandbox` attestation, and `termination_evidence` when available. Launch identity is persisted before the controller authorizes execution; uncertain crashes remain charged and are never blindly replayed. Cooperative mode provides descendant tracking without OS filesystem/network isolation. Strict mode fails with `CAPABILITY_UNSUPPORTED` when its real OS boundary cannot be established; there is no silent fallback. A fresh preflight rejection may attest `no_process_created:true`; failure after process creation must not make that claim. Strict capability must not be reported integration-tested without a real isolation test. Controller credentials never enter the strict child environment. Termination targets only identity-checked owned processes; all descendants must be accounted for or uncertainty remains charged.

## Host integration (adapters/hooks/doctor/installer owner)

`adapters.py`: `build_command(engine, workspace, prompt, executable=None) -> dict {argv, stdin_text, env}`; the base adapter adds no model/reasoning substitutions or permission/trust bypass. The cooperative Codex adapter uses `codex exec --json --sandbox workspace-write --cd PATH -c agents.enabled=false -` and stdin prompt; controlled native jobs instead use the fixed App Server runner described in the 0.4 section below. The installed CLI's help and strict configuration loading recognize these shapes; this is not evidence of an authenticated model turn or live delegation-tool enforcement. ZCode uses `--cwd PATH --prompt=PROMPT --output-format stream-json --workflow-mode disabled --disallowed-tools Agent,Task,Workflow,CreateWorkflow,AmendWorkflow,ResumeWorkflowRun,SendMessage --mode guarded`. ProviderRuntime supplies only an explicitly selected model/quality snapshot and narrow transport configuration; it does not import ambient accounts. Controlled native mode requires its explicitly selected and verified standalone external-sandbox policy; it cannot silently replace an existing native approval policy. Unverified native model jobs remain blocked. `parse_event(engine,line) -> dict|None` validates bounded NDJSON; turn completion is never process-tree termination.

`hooks.py`: `handle(adapter, payload, context=None) -> dict`. Context trusted controller-supplied dictionary; without enrolled project, no policy side effects. Inject English context on supported SessionStart/SubagentStart, deny unmanaged spawning in enrolled controlled project and worker recursive spawning. Never grant permissions. Bound Stop reminders (default no auto-loop unless trusted context proves READY work), unknown/mixed identities rejected. No inferred slot release from native completion hook. Reject contradictory snake/camel aliases. CLI emits JSON only; errors use host-correct deny response when possible and nonzero exit. Native hooks alone cannot guarantee arbitrary shell interception or all host paths.

`doctor.py`: `doctor(engine=None, profile='controlled_executor', *, provider_configuration=None, native_registry=None, project_roots=None) -> dict`; runtime probes plus separate evidence levels. `offline_executor_supported` can be true while model-job `effective_mode` remains `blocked`. `native_deployments` reports fresh record checks for explicit project/engine bindings. Transport status is `native_protocol_verified` only when the selected bindings pass; otherwise `native_acceptance_required`. A valid native protocol record is not real-provider acceptance. Optional configuration is validated without reading credentials; no upstream request or auth prompt is made. Missing binaries or an unavailable boundary also block the relevant capability.

`installer.py`: `build_plugin(adapter, destination, source_root) -> dict` creates new standalone Python-source package containing launcher, xiaolvji modules, English policy/Skill and adapter-specific manifests/hooks; refuse existing nonempty destination. Python 3.11+ prerequisite. No executable .exe claim. `install_policy(project, action='inspect', target='AGENTS.md')` delegates existing ownership-safe injector; global configs/trust never silently edited. Native installation/trust documented separately.

## Root-owned composition

CLI/controller authentication, common validation, rolling asynchronous Dispatcher, exact-artifact checks/integration, self-contained demo, MCP read/planning surface, package metadata, docs and end-to-end tests. Agents own only files listed in their assignment; request contract changes before assuming signatures. Do not commit shared tree independently.

## Implemented refinements after composition tests

The initial interfaces above remain available. The implementation adds:

- `Ledger.block_task(project_id, task_id, reason, principal, request_id)`: persist a quality blocker after confirming no active attempt. A replacement task needs explicit planning; this does not reset retry budgets.
- `Ledger.review(project_id, task_id, artifact_digest, decision, binding, principal, request_id)`: approve/reject/rework, bound to the exact current integration and configured reviewer. Reject/rework blocks the task and revokes applicable history.
- `Ledger.gate(project_id, task_id, gate, artifact_digest=None)`: resolve a current proof, or a valid exact historical integration/review proof. History is fenced by plan, candidate, dependency closure, reviewer and revocation epoch.
- `Ledger.begin_publication(project_id, publication_id, plan_binding, principal, request_id)` and `end_publication(project_id, publication_id, principal, request_id)`: a separate durable single-writer lease. It blocks plan/policy replacement across Git publication and gate recording, survives an actual controller crash, and does not occupy a managed-worker slot. Use fresh request IDs on each recovery; publication_id remains stable.
- `attempt.launch_state`: PENDING/CLAIMED/CANCELLED/DONE. CANCELLED proves an unclaimed outbox entry was cancelled transactionally; absence of a PID file alone does not prove this.
- `status.pending_publications`: outstanding publication records separate from worker occupancy.

Admission selects current proofs for unpinned dependencies and saves the exact `{task_id, gate, artifact_digest}` consumption snapshot. Verification of an already-reserved attempt uses that snapshot. A newer combined delivery does not retroactively change the inputs that an older attempt consumed. Contract/implementation/candidate invalidation still revokes affected proofs. An old exact review never approves a new combined artifact.

A resource domain runs one dispatcher under an OS lock. Unique claims remain necessary even with that lock. The launch boundary rechecks each project's enabled state and exact execution profile; a resumed outbox entry cannot borrow a weaker gateway. Disabled unclaimed reservations pause and remain charged until re-enabled or explicitly cancelled.

Implementation failure with an unclassified nonzero host exit is conservatively blocked by the dispatcher. The ledger enforces retry ceilings and backoff, but budget availability alone is not a diagnosis that repeating an unknown side effect is safe.

`Pipeline` materializes exact reserved dependency artifacts, automatically creates bounded verifier jobs, and integrates a verified frontier when a downstream integration/review gate requires it. Candidate and integration checks preserve the same commit ID as the report. Registration recipes and publication intents are durable outboxes with crash reconciliation. Git CAS, receipt persistence, and ledger gate updates are separate systems; the intent and publication lease make uncertainty visible instead of claiming one impossible cross-system transaction.

The CLI exposes `init`, `project`, `plan`, `task`, `run`, `status`, `queue`, `metrics`, `events`, `cancel`, `contract-ready`, `review`, `policy`, `doctor`, `provider validate`, `host verify`, `package`, `hook`, `mcp`, `check-runner`, and `demo`. MCP exposes six read tools for status/events, queue/metrics and bounded committed source tree/file reads, plus authenticated task registration. It implements the legacy 2025-11-25 initialization protocol, not the 2026-07-28 stateless protocol.

## Implemented 0.2.0 strict offline refinements

### Boundary construction and policy

`sandbox.py` exports `LinuxSandbox(state_dir)`, `probe() -> dict`, and:

```python
prepare(
    argv: list[str],
    cwd: str | Path,
    env: dict[str, str],
    *,
    workspace: str | Path | None = None,
    write_scope: list[dict] | None = None,
    readonly_paths: Sequence[str | Path] = (),
    writable_paths: Sequence[str | Path] = (),
    provider_transport: dict | None = None,
    home_files: Sequence[dict] = (),
) -> SandboxLaunch
```

`SandboxLaunch` is a context manager exposing `argv`, `env`, `pass_fds`,
`provider_fd` and `evidence`; closing it releases its owned descriptors. It constructs a
launch but does not authorize a task, start a process or write a controller
journal. `ProcessExecutor.run(..., sandbox_policy=...)` accepts only the six
trusted policy fields `workspace`, `write_scope`, `readonly_paths` and
`writable_paths`, plus `provider_transport` and `home_files` added in 0.3.0.
An inherited provider endpoint also requires a validated `provider_runtime`
envelope. Worker input must not be treated as an authenticated grant.

The backend is `linux-bubblewrap-offline-v1`. Bubblewrap 0.12.0+ and util-linux
`unshare` are required. The launcher creates an empty network namespace before
bubblewrap constructs the other namespaces, filesystem mounts and seccomp policy.
No host-network mode is available. A bounded real probe establishes runtime
capability; each task still requires its own observed launch attestation.

The source root is mounted at its original absolute path. `write_scope=[]`
provides a read-only checkout. Existing declared directories/files receive narrow
writable overlays; top-level `.git` stays read-only. Missing directory roots must
be prepared by the controller; missing exact files fail closed. Exact-file mounts
permit direct writing but not atomic replacement/deletion of their mount points.
The low-level `None` policy grants the whole private workspace, except `.git`;
managed tasks always supply explicit scopes. Link/special-file preflight and
runtime mount enforcement are complementary. Full details and trust assumptions
are in the [strict sandbox contract](STRICT-SANDBOX-CONTRACT.en.md).

Policy evidence includes `backend`, `policy_digest`, `policy` and observed tool
versions. The canonical policy records its workspace, scopes, exact readonly and
writable mount destinations, `network:"none"`, namespace list, seccomp version,
architecture and private home. It contains no environment values or credentials.
The digest describes requested grants; it is not sufficient evidence that the
boundary was installed.

### Authorization, exit and recovery

The controller first records the supervisor identity through the existing
`on_started` handshake. The supervisor starts only a trusted isolated bootstrap,
which imports no task modules or site hooks and waits for release. The supervisor
checks actual namespaces, zero capabilities, `NoNewPrivs`, seccomp mode, mount
flags and hidden controller paths, then binds the observation to its owned
bootstrap and namespace-init identities. It fsyncs `sandbox.json` before sending
the release nonce. The bootstrap replaces its control input with task stdin,
closes extra descriptors and only then execs the command.

An authorized strict command can release its slot only after the namespace init
has exited and the outer subreaper has reaped all owned children. A returned main
process, native turn event or missing PID file is insufficient. A missing
supervisor result remains uncertain even if its namespace is no longer visible.

Recovery requires `launch.json` to match the durable identity's profile,
attempt, generation and nonce; controlled launches additionally require the same
policy digest. A missing or mislabeled launch record cannot skip strict checks.
For a started strict command, the result must match the durable sandbox record;
claimed full termination must carry both namespace-init and child-reaping proof.
The identity's recorded profile determines recovery requirements, including when
a shared executor root also contains trusted controller-service launches.

`timed_out` and `cancelled` are failure facts independent of the numeric exit
status. The ledger persists the complete terminal observation and returns the
exit payload as `attempt.outcome`. Live handling, candidate submission, quality
checks and `Pipeline.recover_completed()` all reject an exit-0 timeout or
cancellation. A confirmed failed exit may release capacity; it does not earn a
candidate or a quality gate.

With an available execution backend, the dispatcher reconciles existing launches
and unfinished deliveries before admitting new work. New host/model capability
restrictions do not become a reason to skip those stored offline results.
Capability/source preflight runs outside the asyncio dispatch loop, allowing
other completed attempts to release capacity and wake rolling admission.

### Trusted quality service and per-check isolation

`quality_service.py` exports:

- `async run_quality_service(executor, attempt, job, *, on_started=None) -> dict`;
- `validate_strict_report(report, manifest) -> None`, raising `Fault` on mismatch.

Only a controller-created verification/integration recipe for an admitted
verifier/integrator can use this service. It binds the manifest to the workspace,
exact artifact, approved acceptance commands, attempt and generation. The service
launches a fixed installed `xiaolvji.check_runner` command. It never executes an
arbitrary `job.argv` or caller-selected environment outside the sandbox.

The trusted outer runner owns reports, manifests and execution journals. It uses
the existing process tracker and the phase's existing managed reservation. Each
actual task-supplied check is a descendant executed by `controlled_executor` with
`write_scope=[]`, no extra readonly inputs and no writable output grants. This
does not create another model worker or acquire an additional ledger reservation.
Tested code cannot write its own passing report or another check's journal.

`check_runner.py` exposes
`async execute(manifest, *, execution_profile="cooperative_native") -> dict` and
`run_manifest(manifest, *, execution_profile="cooperative_native") -> dict`.
The CLI accepts `check-runner --manifest PATH --execution-profile
controlled_executor`. Direct invocation is a diagnostic API, not automatic
ledger admission. Managed strict jobs use the validated outer service.

Strict manifests put report/log destinations outside source. Checks use private
temporary storage rather than a writable source scratch directory. The report
records `execution_profile`, exact artifact and manifest digests, source snapshots,
environment/tool identity, and per-check outcomes. For each executable check:

- `argv` remains the approved command array;
- `executable` records the resolved executable path and byte digest;
- `executed_argv` is that exact path followed by the approved remaining arguments;
- cwd, timeout, cancellation, process identity and actual sandbox proof are bound
  to the check's identity.

The gateway must run the resolved executable whose bytes were recorded. A
different PATH inside the sandbox must not silently select another same-named
program. If the recorded executable is unavailable under the approved mounts, the
check fails. Evidence does not claim that every transitive dependency is hashed
or that the environment is hermetic.

`validate_strict_report()` rejects cooperative, mutable-source, networked,
unattested, misbound or unsuccessful check evidence. An optional criterion without
argv may be explicitly skipped; required criteria cannot silently disappear.
Exact-artifact validation, integration CAS and designated external review remain
separate gates after process execution.

### Host evidence and remaining unsupported capabilities

The installed Codex `0.167.0-alpha.4` passed configuration loading and actual
native tool turns against a local synthetic provider through the fixed App Server
runner. The older NETLINK_ROUTE failure is historical; ordinary nested native
sandbox tool execution remains restricted, while externally enforced fixture
execution works. Production managed-policy equivalence is not established and
this development host's actual policy inventory blocks standalone admission.
ZCode pure source option mapping was executed against its complete pinned source;
its full CLI/schema build is blocked by npm access. Hook/MCP installation and
real external provider acceptance remain separate. See both host compatibility
documents and the [native host contract](NATIVE-HOST-CONTRACT.en.md).

Scoped provider transport and explicit host snapshots now have synthetic
integration evidence. There is no implicit copying of a user's account or model
configuration. Missing native-host acceptance still blocks real strict model jobs.
There are no cgroup CPU/RAM/process quotas, Windows/macOS strict backends, formal
review approval, signed release or verified remote push implied by this version.
The current validation report identifies what actually ran, which checks were
skipped, and which snapshot was tested; this contract does not freeze a final
test count.

## Implemented 0.3.0 provider composition

`host_config.py` normalizes explicit engine/provider/model/quality snapshots,
derives exact native request constraints and renders readonly private-HOME files,
closed environment overrides and supported selection arguments. See the
[host configuration contract](HOST-CONFIG-CONTRACT.en.md) for every supported
field and source/loader evidence level. The selected snapshot is not proof that
its model supports the supplied values; real host/provider acceptance remains a
separate gate.

`provider_config.py` validates the closed provider/host bindings without reading
credentials. Actual credential reads reject source/workspace locations and every
implicit runtime mount source, using the same mount inventory as sandbox
construction. `provider_broker.py` owns exact HTTPS profiles, request budgets,
local in-flight limits, framed IPC, revocation and durable uncertain outcomes.
`provider_bridge.py` runs only inside the explicitly brokered namespace.

`Pipeline(..., provider_runtime=None)` optionally binds a `ProviderRuntime` to
implementation jobs. It persists non-secret binding/config/quality digests,
hands off the endpoint only after the supervisor identity is recorded, and
revokes on completion, failure, cancellation or shutdown. Verification and
integration recipes never acquire provider grants and retain the exact offline
strict report validation rules above. The dispatcher closes provider authority
before releasing its domain ownership lock.

Provider revocation does not release a worker slot. The process executor still
requires namespace-init exit and complete descendant reaping. Lost workers stay
charged even if their model channel has closed. Provider-side computation after
HTTP disconnection is unknown and is not represented as stopped by local request
accounting. Detailed schemas, budgets, protocols and recovery invariants are in
[PROVIDER-TRANSPORT.en.md](PROVIDER-TRANSPORT.en.md).

## Implemented 0.4.0 native execution and continuous scheduling

`Pipeline(..., provider_runtime=None, native_executor=None, native_registry=None)`
requires fresh `NativeRegistry.check` for strict native implementation jobs before
credentials or grants. It checks source policy, prepares exact dependency inputs
and declared directory roots, then checks the actual workspace policy and rejects
write scopes covering supported instruction files. Provider binding inserts only
renderer-approved `argv_extra` into the closed App Server stdin envelope; it does
not splice CLI-style flags into the wrapper argv. Model/effort must match exactly.

`native_policy.py`, `native_acceptance.py`, `native_registry.py`,
`codex_app_server.py` and `native_result.py` implement the bounded identity,
fixed protocol fixture, controller-owned records, one-turn native runner and
complete semantic result described in [NATIVE-HOST-CONTRACT.en.md](NATIVE-HOST-CONTRACT.en.md).
Production preflight refuses policy sources it cannot preserve, including native
`.agents` skill/plugin trees. Capturing an isolated fixture's success does not
certify the controller host's managed policy. Finished native jobs reload durable
executor output/truncation/cancellation evidence, validate native semantics and
instruction equality, and persist the result digest before candidate submission.
Recovery uses the same checks; an exit-zero failed or truncated turn cannot pass.

`Dispatcher.run(project_id=None, timeout=3600, *, watch=False)` and `run --watch`
keep the queue alive across idle periods without adding planning models or task
scope. Local completion events wake admission immediately; cross-process ledger
arrivals are discovered through polling with an idle wait of at most 250 ms; recovery, scanning and other loop work can add detection latency. A full pool stops admission,
not running work. Unknown exits stay charged. Deadlines are checked before fresh
admission. At timeout, only still-active attempts are cancelled; unadmitted work, completed evidence and project enablement are preserved. Explicit project cancellation retains its separate wider semantics. Repeated LOST reconciliation does
not emit duplicate loss events. Default drain-and-exit behavior is unchanged.

`repository_read.py` supplies `parallel_project_tree` and `parallel_read_file` to
MCP's reader/planner surfaces. The exact commit must be current HEAD or an ancestor
and the project must be enabled with an approved plan before and after the read.
Subsequent pages require a pinned commit. Reads are bounded, UTF-8 and regular
committed files only, with no external protocol, lazy fetch, replacements,
filters or text conversion. Directory descriptors and pre/post identity checks
reject repository/.git replacement races while supporting normal linked worktrees.
Hook exceptions match only this plugin's exact known MCP names; they return no
permission grant and do not substitute for server-side authentication.

## Implemented 0.5.0 fairness and diagnostics

`Ledger.queue(project_id=None, limit=100)` shares the exact admission checks used by reservation, with bounded advisory output and complete-population budgets. `scheduling.py` supplies restart-stable priority aging with a common ceiling and FIFO ties. `metrics.runtime_metrics(ledger, project_id=None)` aggregates a bounded, consistent, read-only event snapshot and explicitly names unavailable measurements. See [SCHEDULING-CONTRACT.en.md](SCHEDULING-CONTRACT.en.md) for fields, ordering, timing populations, integrity checks and limits. The two CLI/MCP tools do not reserve work or grant evidence authority. Initial dispatcher recovery now falls inside the cancellation handler. ZCode prompt values are encoded into one argv element and tested through the real pinned parser; all native readiness gates remain unchanged.
