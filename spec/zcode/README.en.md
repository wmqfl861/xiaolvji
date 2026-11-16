# ZCode adapter: future distribution templates

> These are retained design examples. Build actual runtime 0.1 source plugins with `parallel-dev package`; consult `docs/HOST-INSTALL.zh-CN.md`. Generated packages use implemented Python launchers and current manifests, not the placeholder binaries in these examples.

## Status and evidence

These files specify a future release package. They are **not an installable plugin**. The required `parallel-dev` / `parallel-dev.exe` executable, supervisor, state store, admission service, and sandbox backends are not included in this design package. Renaming the examples does not implement the runtime.

The adapter contract was inspected against ZCode commit `a60ba6a7e5a06742270ca605f5d2cc2766afa7e1`, whose repository README identifies v3.17.1. JSON parsing and structural template checks do not establish compatibility with an installed binary. No ZCode host was launched, no plugin was installed, and no user configuration was changed while preparing these templates.

The pinned implementation takes precedence over older website documentation and descriptive tool strings. In particular, current source supports trusted project hooks, while an older public Hooks page says that project hooks are ignored.

## Release layout

The future packager must produce these paths inside one plugin root:

| Design source | Future release path | Requirement |
| --- | --- | --- |
| `plugin.json.example` | `.zcode-plugin/plugin.json` | ZCode-native manifest; replace the design version only for an actual release. |
| `hooks.json.example` | `hooks/hooks.json` | Use for a validated POSIX package with an executable `bin/parallel-dev`. |
| `hooks.windows.json.example` | `hooks/hooks.json` | Use for a validated native Windows package with `bin/parallel-dev.exe`. |
| `../parallel-development.SKILL.md` | `skills/parallel-development/SKILL.md` | Copy the complete English Skill source during packaging. |
| Built runtime artifact | `bin/parallel-dev` or `bin/parallel-dev.exe` | Supply the correct target platform and architecture; verify provenance and executable permissions. |

ZCode automatically discovers the standard `hooks/hooks.json` file. The manifest intentionally has no `hooks` declaration, so the same hook source is not registered twice. The future installer must select **one** platform hook file for that standard path. The examples are alternatives, not two simultaneously enabled hook sets.

ZCode supports its own `.zcode-plugin/plugin.json` manifest and a compatibility `.claude-plugin/plugin.json` fallback. This adapter uses the native path. Do not copy the Codex manifest format, `extensions` fields, or Codex hook event names into it.

## Platform selection and path handling

All handlers use `type: "process"`, a single executable in `command`, a separate `args` array, and `timeoutMs`. The host expands `${ZCODE_PLUGIN_ROOT}` and executes the command through its argv execution path. Do not add shell quotes around `command`: quotes would become part of the executable filename. Do not concatenate a user prompt into a shell command.

The pinned `HookProcessConfigSchema` has no `commandWindows` or platform override field. A future packager or installer must select the POSIX or Windows template; it must not invent an in-file platform selector. Each target package needs its own platform test, including a plugin directory containing spaces and non-ASCII characters.

The inspected Windows argv adapter normalizes paths and executes native `.exe` programs without a shell. The Windows example therefore names `parallel-dev.exe` explicitly. This design does not depend on `.cmd` shims, PowerShell, Bash, or extension inference. If a future release uses an interpreter, the installer must resolve and validate that interpreter and generate its `command` and `args` accordingly.

The POSIX example targets a native executable with the executable bit set; the Windows example targets a native Windows executable. Source-level path handling is verified, but these future binaries and either template have **not** been executed on Linux, macOS, or Windows. Do not advertise platform acceptance until the corresponding host and sandbox tests pass. A Windows filesystem path serialized directly into JSON must use JSON escaping; let a JSON serializer generate it.

Runtime state belongs in `${ZCODE_PLUGIN_DATA}` or another explicitly configured supervisor state location, not in the installed version directory. Use the environment values supplied by the host; do not derive identities from an untrusted prompt or a model-authored completion message.

## Event coverage

The pinned host supports seven hook events. This adapter registers six, all synchronously:

| Event | Adapter responsibility | Boundary |
| --- | --- | --- |
| `SessionStart` | Inject the English policy and the selected adapter mode; reconcile persisted run identity. | Policy injection is context, not a filesystem sandbox. |
| `UserPromptSubmit` | Reconcile the user's authorized objective and provide scheduling context. | Do not expand scope or treat user text as an authenticated control-plane event. |
| `PreToolUse` | Check admission, managed task identity, resource ownership, and supported tool arguments. | It covers only calls that actually pass through this hook runner. |
| `PostToolUse` | Record the actual result and reconcile launch acknowledgements. | Background `Agent` output acknowledges launch; it is not a completion signal. |
| `PostToolUseFailure` | Record failure and reconcile any reservation against authoritative runtime state. | An error alone does not prove that a worker or its process tree has stopped. |
| `Stop` | Supply a bounded continuation reason if authorized, executable work remains. | Respect cancellation and the host's consecutive continuation limit. |

`PermissionRequest` is deliberately omitted. This plugin does not grant permissions, override an existing denial, or automatically switch permission modes. `PreToolUse` should normally return an empty JSON object when it has no objection, preserving the host's normal permission evaluation; it must not return `allow` merely because a task lease exists.

The event matchers are intentionally omitted so that every event of each registered type reaches the adapter. The handler must distinguish an active managed run from unrelated work and evaluate the concrete tool schema. A short list such as `Write|Edit|Bash` is not a complete list of ways a tool can mutate state. Unknown side-effecting tools inside a managed strict run must be rejected by that mode's trusted execution boundary, not guessed safe.

ZCode does not expose `SubagentStart`, `SubagentStop`, or `Interrupt` in this pinned hook union. Do not manufacture these events. Reliable cancellation and completion for strict-mode workers must come from the supervisor's process or host lifecycle adapter. Hook callbacks must return promptly; they must not run the worker pool, perform long waits, or recursively spawn more agents.

## Hook protocol

The future executable is invoked as:

```text
parallel-dev hook --adapter zcode
```

It reads the event JSON from stdin, dispatches using `hook_event_name` / `hookEventName`, and writes at most one valid response JSON object to stdout. Write diagnostics to stderr. Keep responses within the host's configured output limit, and do not include secrets, whole transcripts, or the full scheduler database in context.

Useful input aliases include `session_id`, `cwd`, `tool_name`, `tool_input`, `tool_use_id`, `tool_response`, and `stop_hook_active`. The host also supplies camelCase fields. Preserve native IDs and validate required fields for each event instead of guessing an identity when one is missing.

English policy injection for a session start:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Use the parallel-development Skill for the authorized task. Dispatch only dependency-ready work under the configured resource budget. Preserve the user's permission boundaries."
  }
}
```

A concrete denial for a managed operation:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "This managed operation has no valid execution lease. Reconcile the task state before retrying."
  }
}
```

An event with no additional action returns `{}`. ZCode does not treat arbitrary plain stdout as injected context. If `updatedInput` is ever used, it must be a complete replacement accepted by the tool's schema; do not add invented `task_id`, `cwd`, or `worktree` fields to native `Agent` input. Carry task metadata in supported prompt fields and the trusted state store.

For `Stop`, `{ "decision": "block", "reason": "..." }` requests another turn. The pinned runtime limits consecutive Stop continuations to three. The adapter must respect `stop_hook_active`, cancellation, an empty eligible queue, resource saturation, unresolved dependencies, and circuit-breaker state. Stop is an advisory continuation boundary, not the scheduler's event loop. A full pool should wait for a real completion event; it should not repeatedly force model turns.

## Failure behavior and trust

The template sets `onError: "deny"` only on synchronous `PreToolUse`. At the pinned commit, an executed handler that fails or times out is translated into a denial for that tool call. A JSON object that parses successfully but fails the host's output schema also raises a hook error. Exit code 2 is a supported blocking path. Other hook events do not acquire this guarantee by setting the same property.

There is a material parsing exception: `parseHookStdout()` silently returns no result for empty stdout, text that does not start with `{`, or a JSON parse failure. With exit code 0 these cases do **not** trigger `onError: "deny"`. Thus the host hook protocol is not universally fail closed for malformed output. The future adapter must validate and serialize its complete response before writing it, exit with a blocking failure when it cannot produce a valid response, and test this distinction explicitly. Strict execution must still be admitted by the external supervisor or another verified execution boundary; it cannot rely solely on ZCode converting arbitrary stdout corruption into a denial.

The future installer must probe the installed host's actual execution-error, timeout, parsed-schema-error, and malformed-stdout behavior with inert diagnostic calls before describing its admission guarantees. An older host may not support `onError`, or may ignore an unknown field. Do not silently remove this setting and continue to advertise the same enforcement. If a required check fails, report the capability gap and disable managed admission or offer an explicitly labeled advisory mode. Do not compensate by weakening permissions or enabling yolo.

`onError: "deny"` applies to a hook that was actually executed. It does not make an unloaded plugin, an untrusted skipped project hook, user cancellation, or a child runtime without hooks into a protected boundary. The independent supervisor must refuse new strict-mode worker admissions while its required control-plane health checks fail. A hook error does not authorize releasing an occupied slot; reconcile the actual worker and process tree first.

Plugin hooks follow plugin loading and enablement. A user runtime hook switch is not proof that every plugin hook is disabled. Confirm the actual loaded declarations and diagnostics in a new session.

Current project hooks have a separate trust flow. When a user chooses to put hooks in project runtime configuration, the documented sequence is review, inspect the concrete commands and source, then grant the exact reviewed digest:

```text
zcode hooks trust review --workspace /absolute/project --json
zcode hooks trust grant --workspace /absolute/project --hook-digest REVIEWED_SHA256
zcode hooks trust status --workspace /absolute/project --json
```

These lines describe the host's trust interface; this design does not run them or grant trust. Opening a project, setting `hooks.enabled`, or changing permission mode does not grant project hook trust. Changed declarations require a new review. A skipped `SessionStart` is not replayed in the current session. Do not bypass this trust mechanism or treat a corrupt trust store as authorization.

### Optional runtime configuration fragment

`runtime-hooks.json.example` is a narrow fragment for a user who has already chosen to enable runtime-configured hooks. It is not a replacement configuration file and is not required merely to demonstrate this plugin template.

A future installer must read and parse the existing configuration, preserve every unrelated top-level field and every existing `hooks` field, and change only the requested `hooks.enabled` value. Keep existing `events`, limits, and declarations intact. Show the resulting change before any required approval, and support restoration. Do not register the plugin's standard hook file again in `hooks.events`.

If existing configuration cannot be parsed, stop that edit and report the parse error. Do not replace it with this fragment. Enabling hooks does not grant project hook trust and does not alter tool permissions.

## Child-agent and scheduling compatibility

At the pinned commit, ordinary `Agent` child runtimes are constructed without the parent's hook configuration or hook runner. Parent tool-event mirroring does not close this gap. The ordinary child also inherits the parent working directory, has no native `cwd` or `worktree` argument, and cannot spawn further subagents. English AGENTS instructions or a Skill remain behavioral guidance in that path.

Consequently, native collaboration mode may use task contracts, bounded dispatch, completion notifications, independent reviews, and worktree conventions, but must not claim that these hooks enforce every child write. `SendMessage` can resume a terminal child in the background; admission accounting must handle resumes as well as new `Agent` calls. A copied `<task-notification>` string is not an authenticated completion record.

The newer `CreateWorkflow` engine provides a real queue, `max_concurrency`, and immediate refill when an active ask settles. Its actor contexts can remain alive while idle, so its cap is not a count of all surviving contexts. Its process-wide governor limits provider/model requests and is not a cross-process worker semaphore. It offers no per-actor worktree isolation option. Workflow children inherit runtime configuration differently from ordinary Agent children, but this must be capability-tested; do not generalize that behavior to ordinary Agent or to every direct workflow command surface.

The older `Workflow` tool is a separate API. Its description mentions worktree isolation and nested workflows, but the pinned implementation throws for both. Do not use those descriptive claims as release capabilities.

Strict mode requires the future external supervisor, independent worker processes, verified sandbox boundaries, centralized resource leases, authoritative lifecycle events, and serialized integration. A Git worktree separates normal file paths; it does not prevent out-of-tree writes. An independent ZCode CLI worker can use the verified `--cwd`, `--prompt`, `--output-format stream-json`, `--workflow-mode disabled`, and `--disallowed-tools` flags, but these flags alone are not a sandbox. Headless `-p` defaults to yolo in this source, so select an intentional permission mode and preserve existing restrictions. Do not change to yolo to make denied work succeed.

## Validation required before release

The implementation must validate the native plugin manifest and source discovery, one hook invocation per intended registration, JSON protocol output, explicit denials, missing executables, timeouts, corrupt responses, output-size limits, platform paths, and preservation of existing permissions and configuration. Test ordinary Agent and CreateWorkflow child coverage separately, and report their actual guarantees.

Exercise background launch versus completion, resumed agents, cancellation, worker crash recovery, duplicate events, lease reconciliation, and saturation without busy loops. Strict-mode acceptance also requires sandbox escape-boundary tests, process-tree termination confirmation, and conflict-free serialized integration. Until these checks pass on the installed host version, label this adapter a design template rather than an accepted runtime.

## Pinned source references

- [Hook events, process schema, output schema, and onError](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/hooks/index.ts)
- [Standard plugin hook discovery](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/plugins/hook-sources.ts)
- [Plugin environment variables and placeholder expansion](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/hooks/configured-runner-input.ts)
- [Process hook invocation and stdout handling](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/hooks/configured-runner-callback.ts)
- [Execution argv and Windows native executable handling](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/exec/execution-command.ts)
- [Windows path normalization](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/adapters/src/exec/windows-executable.ts)
- [Current project hook trust documentation](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/docs/usage/09-hooks.md)
- [Ordinary Agent child runtime construction](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/runtime/methods/subagent.ts)
- [Hook runner creation conditions](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/runtime/helpers/runtime-tools.ts)
- [CreateWorkflow input contract](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/tools/create-workflow.ts)
- [Dynamic scheduler slot release and refill](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/dynamic-workflow/src/engine/scheduler.ts)
- [Legacy Workflow implementation limitations](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/bootstrap/src/app/script-workflow-runtime.ts)
- [CLI arguments](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/arguments.ts)
- [Headless default mode](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/cli/src/run.ts)
