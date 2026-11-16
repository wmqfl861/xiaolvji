# Native host execution contract — 0.5.0

This contract separates native protocol evidence, deployment policy, real provider
acceptance, process termination, and project quality. No one of these facts
substitutes for another.

## Supported deployment and identity

`native_policy.normalize_native_deployment(value)` and
`load_native_deployment(path)` produce an immutable `NativeDeployment`.
The closed version-one descriptor contains `schema_version`, `engine`,
`executable`, `protocol`, and `policy`. Codex uses `codex-app-server-v2` with
`context=standalone-api`, `system_policy=must-be-absent`, and
`approval_policy=on-request`. ZCode's `zcode-cli-v1` descriptor uses
`mode=guarded`, but its complete runtime identity is not yet supported.

`capture_native_identity(deployment)` runs no selected host executable. It
requires a canonical static ELF64 Codex executable, rejects wrappers, dynamic
dependency closures, aliases and unsafe file permissions, and captures bounded
streaming hashes with before/after/name checks. The record binds runtime Python
source and packaged resources, Python version, kernel/architecture/boot identity,
and the fixed trusted bubblewrap/unshare binaries and their versions.

The initial Codex profile checks the controller host's `/etc/codex` with metadata
operations before native startup. Presence, aliases, or an unreadable inventory
block; private HOME absence is never used as proof of host-policy absence.
Existing user/account/cloud policy migration is not implemented. An explicitly
selected standalone profile must not be used where an existing policy applies.

`capture_project_policy(project_root, workspace=None)` rejects native `.codex` and `.agents`
entries (including skill/plugin sources), root `hooks.json`, and omitted ancestor policy/instruction sources.
It reads only in-tree `AGENTS.md` and `AGENTS.override.md` as bounded, stable
regular files, binds their relative paths and exact bytes, and compares them
with the actual workspace when supplied. Normal code changes do not invalidate
the policy digest. Instruction changes do. Worker grants cannot include existing
instruction files; preflight runs after declared directories are created, and
the pipeline checks project instructions again before candidate submission.

## Native protocol fixture and registry

`native_acceptance.verify_native_host(deployment, snapshot, output)` is a fixed,
credential-free local fixture. It exercises the production Codex runner through
the real strict executor, inherited provider channel, private loopback bridge,
and validating broker. Its synthetic provider returns an actual `exec_command`
call, observes the tool result in a second request, and completes a turn.

The fixture checks scoped writes, read-only sibling and `.git`, external network
denial, hidden controller data, model/quality constraints, missing native
delegation tools, a complete native transcript, and process-tree termination.
This function is a diagnostic test seam, not a production bypass or external
provider. It does not create a readiness record on its own.

`NativeRegistry(state).verify(project_id, project_root, deployment,
configuration, output)` holds the domain dispatcher lock, refuses existing
active/uncertain attempts, captures policy and identity before and after the
fixture, requires a closed versioned report with every exact Boolean assertion,
integer invocation/request counts, complete semantic evidence and a matching
strict executor identity/policy digest, and writes a controller-owned record. It never opens provider credential references. `check(...)` reads that
record and captures current identity/policy again before every native launch.
Any changed code, platform, configuration, or instructions invalidates it.
Records are protected by the existing controller trust boundary; they are not
cryptographic attestations against an already privileged outside same-user
process.

`host verify` is operator-only. There is no readiness import flag, caller-chosen
success Boolean, policy-ignore switch, or implicit fallback. `doctor` reports
matching deployment records and concrete failures, instead of an unconditional
global native-host blocker. A protocol pass still reports
`real_provider_verified=false`.

## Codex App Server adapter

`codex_app_server.py` is a self-contained standard-library runner sealed into
private HOME. Its bounded JSON stdin carries the exact native binary, workspace,
prompt, model, effort, and renderer-produced configuration overrides. It launches
only that absolute executable and exchanges bounded newline JSON-RPC over pipes.
It does not receive the upstream credential or broker socket descriptor.

The runner initializes, reads effective requirements/configuration, validates
the exact model and effort against `model/list`, starts one ephemeral thread,
checks its effective settings, and starts one turn using the documented
`externalSandbox` policy with restricted network access. The surrounding strict
executor enforces filesystem/process/network access throughout. No command is
released before supervisor attestation. The adapter disables native delegation
and provider-managed web search, preserves every explicit supported quality
value, and refuses unavailable or unverifiable configuration.

This is an explicitly selected externally enforced policy, not an assertion that
all ordinary native sandbox approval semantics carry over. Real probes showed
that external mode need not prompt for native escalation/rule requests. The
outer scope still does not expand. Existing managed deployments remain blocked;
neither readonly copies alone nor the partial `configRequirements/read` response
prove equivalent enforcement. See the official
[App Server interface](https://learn.chatgpt.com/docs/app-server) and
[permissions documentation](https://learn.chatgpt.com/docs/permissions).

Server approval, interactive input, credential-refresh, and unknown client
requests are denied or terminate the turn. The adapter never auto-accepts them.
Native errors, model-metadata fallback, mismatched identities, additional turns,
and delegation events cannot become success. After a confirmed completed turn,
the resident service is closed with bounded EOF/termination escalation. The
actual signal and service exit status remain observable; a wrapper success does
not claim that an intentionally stopped service returned zero. An unexpected
service failure before completion is a failed run. Only the outer executor's
durable tree proof can release capacity.

## Native semantic result and recovery

`verify_host_output(engine, stdout_path, truncated=False,
expected_session_id=None)` requires a complete stable regular stdout file. It
rejects aliases, malformed/ambiguous JSON, nonfinite values, oversized lines,
missing identities, multiple turns, errors, and truncation. Limits are 4 MiB
total, 1 MiB per event, and 65,536 events.

Codex requires one `thread.started`, one `turn.started`, and a final
`turn.completed`; native error items cannot be hidden by a later success.
ZCode requires matching session/turn identifiers, `turn.completed` with
`payload.resultType=success`, and a matching final `result` whose projection is
idle/completed. Exit zero and idle alone are insufficient: cancelled or exhausted
ZCode turns can otherwise have both.

Pipeline obtains the stdout path and explicit truncation/timeout/cancellation
facts from `ProcessExecutor.reconcile`, including during restart recovery. It
validates native semantics before creating a candidate, saves a digest-bound
native report, and binds its digest into the candidate. Reports deliberately say
`process_tree_terminated=false`: they have no lifecycle authority.

## Continuous scheduling and coordinator tools

`Dispatcher.run(project_id=None, timeout=3600, watch=False)` preserves drain-and-
exit behavior by default. Watch mode waits across idle periods and uncertain
instances, checks cross-process ledger arrivals every at most 250 ms, and still
reacts immediately to local completion events. Deadlines are checked before new
admission, including after recovery/discovery. Timeout cancels only attempts
that are still active, preserving queued work, completed evidence and project
enablement. Explicit project cancellation disables the project and revokes its
related task/history gates; it does not itself terminate an existing watch
process. Controller interruption retains that wider cancellation behavior. No
unbounded Stop-hook continuation or extra planning model is created.

MCP exposes four reader tools: status, events, bounded committed directory pages,
and bounded committed UTF-8 file reads. Task registration remains authenticated
planner/coordinator/operator-only. Source reads require an enabled approved
project and a current-HEAD/ancestor commit, reject dangling/unrelated objects,
symlinks, gitlinks, binary/oversized content, filters, replacements, lazy fetch,
and external protocols. Exact plugin MCP names may pass the coordinator Hook
without claiming a worker identity; the host's normal policy and the server's
own authorization still apply. Arbitrary similarly suffixed tools do not match.
