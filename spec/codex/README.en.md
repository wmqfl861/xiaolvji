# Codex adapter design templates

> These are retained design examples. Build actual runtime 0.1 source plugins with `parallel-dev package`; consult `docs/HOST-INSTALL.zh-CN.md`. Generated packages use implemented Python launchers and current manifests, not the placeholder binaries in these examples.

## Status and scope

These four files specify a proposed local Codex adapter. They are design templates, not a working plugin release. The shared executable `bin/parallel-dev`, its Windows counterpart, task ledger, native-host adapter, permission enforcement, and installation workflow have not been implemented by these templates. **Do not install or trust this directory as a finished plugin.** Successful JSON parsing does not validate host behavior.

The target is local Codex orchestration with local hook execution. Work Cloud uses a different managed integration path; copying these command hooks into a cloud conversation does not establish support. The installed Codex version and its supported events must be probed before producing an installable package. [Official hook reference](https://learn.chatgpt.com/docs/hooks)

## Intended package destinations

| Design file | Destination in a future built package | Purpose |
| --- | --- | --- |
| `plugin.json.example` | `plugin.json` | Portable identity and OpenAI hook declaration |
| `plugin.compat.json.example` | `.codex-plugin/plugin.json` | Compatibility manifest for hosts using the older format |
| `hooks.json.example` | `hooks/codex.json` | Synchronous lifecycle handlers |

Both manifests use the same name, design version, description, and hook path. Portable packages discover skills in `skills/`; the compatibility manifest declares that path explicitly. If the portable manifest contains `extensions.com.openai`, its OpenAI settings replace rather than merge the compatibility overlay. Maintain both representations from one source during a future build. [Official package reference](https://developers.openai.com/plugins/build/plugins)

The command strings express the intended plugin-root paths. The future installer must verify host placeholder expansion and platform quoting, including paths containing spaces and non-ASCII characters. If the selected Windows execution shell does not expand `${PLUGIN_ROOT}`, render a correctly quoted absolute executable path in the installed configuration. Do not assume POSIX shell syntax is portable merely because a `commandWindows` field exists.

## Shared runtime entry point

Every handler invokes the proposed entry point:

```text
"${PLUGIN_ROOT}/bin/parallel-dev" hook --adapter codex
```

The Windows override selects `parallel-dev.exe`. All handlers are synchronous. `timeout` values are seconds: 10 for normal events and 3 for interruption handling. These are proposed operating budgets, not measured performance results. Normal handlers should perform short local checks or enqueue work; they should not wait for an entire build, review, or model call.

The runtime must read one event object from standard input, dispatch by `hook_event_name`, and emit only the output schema accepted for that event. Diagnostic text belongs on standard error. The templates deliberately contain no simulated allow/deny results and no invented native tool arguments. [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)

## Event responsibilities

The following responsibilities are this plugin's proposed policy, not built-in Codex behavior.

| Event | Proposed runtime responsibility |
| --- | --- |
| `SessionStart` | Reconcile persisted state and inject a bounded English policy summary. |
| `SubagentStart` | Bind a confirmed native identity where available; inject the assigned English task contract. |
| `PreToolUse` | Validate supported operations against task scope and atomically reserve resources before a controlled dispatch. |
| `PostToolUse` | Record the actual result and enqueue reconciliation or scheduling work. Do not pretend a completed side effect was prevented. |
| `Stop` | Request a bounded continuation only when authorized work is executable now. Allow completion, cancellation, and genuine external blocks. |
| `SubagentStop` | Record a report or turn-stop event. Keep closure and resource-release decisions separate. |
| `Interrupt` | Record cancellation promptly. Do not restart the turn or dispatch replacement work after cancellation. |

The future adapter must distinguish event-specific output contracts. It must not use one generic `continue` response for every event. It must also make repeated event processing safe, serialize competing resource claims, and avoid running a full replan after every harmless read.

## English instructions and skills

All injected policy, task contracts, runtime instructions, and hook-generated continuation reasons must be in English. User-facing conversation language remains independent of that requirement.

The future project installer should use the canonical `AGENTS.md` filename. Preserve existing content and insert only a clearly marked managed block. Detect `AGENTS.override.md` and nested project guidance before installation; do not manufacture an override file to force precedence. Record the original file hash and remove only the managed block during uninstall. [AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)

Keep detailed workflow in the packaged skill. Avoid also copying the same skill into a project discovery directory when the plugin already exposes it. Skill metadata and explicit invocation guide selection; injecting a policy summary does not prove the model executed every skill step. [Skill discovery](https://learn.chatgpt.com/docs/build-skills)

## Required capability probes

Before enabling a future build, its `doctor` operation must record the host version and test the following in a disposable fixture:

1. Manifest selection, skill discovery, and the effective instruction-file chain.
2. Each configured event, its actual input fields, and accepted output behavior.
3. Synchronous denial on each supported operation the plugin plans to gate.
4. Duplicate events, handler errors, malformed output, timeouts, and interrupted operations.
5. Native creation, stop, close, and capacity-release semantics.
6. Executable resolution and quoting on the selected operating system.
7. Multiple hook sources, plugin trust state, and coexistence with existing project hooks.

An unsupported required capability must produce a visible unsupported or cooperative-only status. It must not silently claim strict enforcement. Keep the report tied to the tested host version.

## Identity, workspace, and lifecycle limits

Native tool hooks do not by themselves establish a separate worker sandbox. Do not infer worker identity solely from `session_id`, infer isolated ownership from `cwd`, or infer thread closure from `SubagentStop`. Some tool paths do not run the same pre-tool hook, and certain invalid hook outputs can leave the underlying operation running. [Hook coverage and protocols](https://learn.chatgpt.com/docs/hooks)

The shared runtime design must therefore:

- Keep task ID, attempt ID, native agent ID, tool-call ID, and workspace ID separate.
- Bind identities from supported host facts; treat absent identity as unknown.
- Never add an undeclared field to a native creation-tool request.
- Use native closure confirmation or a controlled executor's confirmed termination before releasing the associated capacity.
- Retain handover evidence while validation and independent review remain pending.
- Enforce strict write isolation through a controlled executor or sandbox; label native guidance-only isolation accordingly.
- Respect the host's actual concurrency budget, including open idle threads when applicable.

Current documented Codex concurrency configuration counts concurrently open spawned threads and excludes the primary thread. Custom agent instructions inherit rather than replace the parent sandbox and permission settings. The adapter must preserve those controls. [Subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents)

## Installation and trust

A future installer must preserve existing configuration, avoid registering the same handlers both inline and through a hook file, and obtain the host-required hook trust. Installing a plugin does not automatically trust its scripts. Script changes can require renewed review. Do not modify trust records or weaken host approval settings to make installation succeed. [Plugin hooks](https://developers.openai.com/plugins/build/plugins)

No live configuration, trust state, skill installation, or host concurrency setting is changed by this specification directory.
