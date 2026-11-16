<!-- parallel-development:managed-policy:begin version="1.0.0" -->
## Parallel Development Policy

Use the installed `parallel-development` Skill and the actual `parallel-dev` runtime for enrolled projects. Check capabilities with `doctor`, inspect committed source through the bounded coordinator MCP tools, register scoped tasks, and run the managed queue. Loading this block alone does not enroll a project or grant hook trust. Runtime 0.5 requires fresh controller-owned native deployment acceptance; unsupported management policies, unknown models, changed quality settings, or changed instructions block. A synthetic native protocol pass is not a real-provider or managed-policy acceptance. Use `run --watch` for an authorized session that must keep accepting eligible work across idle periods, with an explicit timeout.

### Respect authority and scope

Follow the host's instruction hierarchy, the user's current request, and applicable project rules. This policy adds a development workflow; it grants no new permissions and cannot override a cancellation or an approval boundary. Work only within the approved plan. Propose additional features separately. Use English for plugin-provided instructions and operational messages; continue answering the user in their preferred language. Preserve the user's original task text when needed as clearly identified data.

### Establish the execution profile

Inspect the actual host, available tools, effective instructions, and capability record before delegating. Never invent a tool, Hook event, argument, permission, lifecycle notification, or successful action. Select one declared profile. In `cooperative_native`, use controller-owned CLI workers and the shared ledger within the explicitly accepted same-user trust boundary; acknowledge coverage gaps and never create a second native queue. In `controlled_executor`, route work through the controller-owned execution gateway and verified isolation. If a required capability is unavailable, block that execution path; use a weaker profile only when the user has explicitly selected or already authorized it. Do not claim strict isolation merely because a Hook is installed.

For ZCode, use trusted project or plugin Hooks only where the reviewed version supports them. Ordinary Agent children do not inherit their parent's Hooks. Upstream `CreateWorkflow` can provide a rolling queue, but this runtime does not integrate that queue or its capacity. Enrolled projects use the single controller-owned queue; do not start native workflows alongside it. For Codex, do not infer a worker's identity or writable scope from a shared parent session identifier. Validate working-directory and lifecycle behavior explicitly. Map logical roles to the topology the host supports.

### Keep provider authority scoped

Use provider transport only through explicit controller configuration and an admitted attempt. Keep upstream credentials in the controller. Never import ambient host accounts, login state, credentials, or model settings into a worker. The controller supplies one unnamed socketpair per attempt; its isolated bridge serves only private loopback HTTP. Do not grant host networking, a shared proxy, or a host service socket. Keep verification and integration checks offline.

Require an immutable explicit model and quality snapshot, read-only private host configuration, and matching broker constraints. Do not substitute a model, lower quality, or change trust, sandbox, or approval settings to make a job run. Respect request-count, request/response-byte, deadline, and local in-flight limits separately from development slots. Revoking a grant closes local transport; it proves neither worker process-tree exit nor termination of remote provider processing. Reclaim development slots only from the executor's confirmed tree-exit evidence.

### Define eligible work

Before dispatch, register a unique task, approved plan revision, objective, baseline revision, contract version, dependency gates, writable scope, resource requirements, acceptance criteria, and expected evidence. Admit a task only when its inputs are available, its scope is compatible with active work, its resources can be allocated, and independent execution has a reasonable benefit. In queue diagnostics, eligible covers state and dependencies; dispatchable additionally checks capacity, scope and resources against the current snapshot. Distinct filenames do not prove semantic independence.

Use explicit dependency gates: a frozen contract may permit implementation with a test double; verified implementation, verified integration, and final acceptance are separate gates. Bind evidence to the exact artifact and contract revision. Reevaluate affected tasks when those revisions change. Do not expand ownership or relax acceptance criteria to keep a worker busy.

### Dispatch continuously within real limits

Use the authoritative controller ledger, not estimates remembered in conversation. Reserve the applicable capacity and exclusive resources atomically before starting work. Include all managed descendants and validation work in the appropriate budgets. Account for the host's actual distinction between open threads, running processes, and model requests. Do not bypass limits by recursive delegation or by launching unregistered workers.

After a meaningful task, dependency, resource, or validation event, refresh eligibility and dispatch compatible ready work while capacity remains. When full, stop new admission and let existing work continue. Process each result as it arrives; do not impose an unnecessary whole-batch barrier. Assign priorities to critical dependencies and accumulated validation or integration work while respecting the controller's bounded aging order. Use the read-only queue diagnostics to distinguish eligibility from current capacity and conflict blockers. Treat every dispatchable result as an individual snapshot observation, not a reservation or a promise that all listed tasks can run together. Use recorded metrics with their stated populations and unavailable measurements; never claim speedup or quality from occupancy alone. Allow idle capacity when no useful eligible task exists. A watch process waits for newly registered approved work; it does not create requirements or wake an exited coordinator session.

### Isolate changes and preserve evidence

Give each writer an identified workspace, branch, source baseline, and build/output directories. Allocate ports, databases, and other mutable resources separately. Respect write ownership and frozen contracts. Ask the coordinator to resolve a necessary scope change before editing outside the assigned scope. Serialize updates to a shared integration reference and validate the actual combined revision. Do not overwrite user changes or use destructive cleanup to remove conflicts.

Submission, execution termination, thread closure, and final acceptance are different facts. Release capacity only according to confirmed host occupancy; release exclusive resources only after the old writer has stopped or lost effective access. Make release operations idempotent. Reject stale attempts and preserve candidates, logs, and handovers through verification and authorized cleanup. Before submitting a native candidate, also require a complete successful host transcript; exit zero, a model completion claim, or an idle projection alone cannot establish success.

### Validate, recover, and stop correctly

Keep established quality gates. Report actual checks and distinguish passed, failed, and skipped results. Worker self-tests and verifier findings are technical evidence; they do not replace the configured independent review process. Only the authorized review path can record final acceptance for the exact delivered revision.

Use an explicit retry budget. Unless the approved configuration is stricter, allow at most two automatic retries after the first attempt. Reconcile uncertain launches before retrying, diagnose repeated failures, and never repeat an irreversible action blindly. Respect user interruption. End scheduling when authorized work is complete; pause with specific reasons when progress requires external input or review. Never use a Stop Hook to create an unbounded continuation loop.
<!-- parallel-development:managed-policy:end -->
