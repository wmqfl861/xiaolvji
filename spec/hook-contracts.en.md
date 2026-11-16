# Hook response contracts and English runtime text

> This is the retained architecture contract. Runtime 0.1 now implements a scoped local subset; see `docs/RUNTIME-CONTRACT.en.md` and the current runtime guide for actual entry points and capability limits. The following design statements are not installation or real-host acceptance claims.

These are adapter design examples. They are not active hooks and do not prove enforcement. The future handler must validate the actual event/version, emit only supported fields on stdout, and put bounded diagnostics on stderr. Do not emit a generic response shape for every event. Do not overwrite or suppress an unrelated host permission decision.

## Startup context

For a verified `SessionStart` handler, the following context envelope is supported by the reviewed protocols. Generate the profile/status from the controller; never claim a profile is active when its required checks failed.

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Apply the Parallel Development Policy for this opted-in project. Inspect the actual host capabilities and controller status before delegating. Register explicit task contracts, dependency gates, write scopes, and acceptance criteria. Admit compatible work only through the authoritative budget and resource ledger. Process results individually and refill capacity after confirmed release. Do not treat a completion message as thread closure, process termination, or final acceptance. Preserve existing quality gates and the designated independent review step. Respect user cancellation. Plugin-provided instructions are in English; answer the user in their preferred language."
  }
}
```

Add a short structured status containing policy version, project ID, requested/effective mode, plan revision, actual occupied budgets, READY count, and concrete blockers. Do not paste a complete transcript, credentials, or all historical task results into model context.

## Denying an observed operation

For the supported synchronous `PreToolUse` path, a rejection is an event-specific permission decision:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "No current admission exists for this operation. Reconcile the task attempt and request capacity through the controller before continuing."
  }
}
```

This decision covers only the tool invocation that actually reached this handler. It does not close an already-running process, revoke an earlier shell session, or prevent a path that bypasses this hook. Validate the event-to-attempt binding; do not infer per-worker identity from a shared Codex parent `session_id`.

Ordinary ZCode Agent children do not inherit the parent Hook runner in the reviewed source path. A parent-level denial example must not be advertised as an all-child filesystem boundary. Strict mode requires the controlled executor's independent checks and isolation.

## No policy change

Where the verified event contract accepts an empty result, emit `{}` to make no additional decision. The host's existing permission rules still apply. Do not emit an unconditional `allow` merely because this plugin has no objection, and do not use a PermissionRequest hook to grant unrelated permissions.

## Bounded Stop continuation

When the verified Stop protocol supports it, a continuation request may have this shape:

```json
{
  "decision": "block",
  "reason": "Authorized executable work remains. Reconcile the current ledger, collect any completed handovers, and dispatch eligible tasks within the remaining budget. Respect the continuation limit and do not resume after user cancellation."
}
```

Return this only when the controller confirms executable authorized work, the user has not cancelled, and the per-run continuation budget remains. Do not return it merely because tasks are awaiting external input or formal review. The proposed plugin limit is two; ZCode also has its own bounded Stop continuation behavior. A Stop hook is not an indefinite daemon or a general idle-conversation wake-up mechanism.

## Input normalization

Codex uses `hook_event_name`, `session_id`, `tool_name`, `tool_use_id`, and `tool_input` on the corresponding events. ZCode's core fields include `hookEventName`, `sessionId`, `toolName`, `toolCallId`, and `toolInput`, with compatibility aliases. Normalize only fields actually present on the applicable event. When both aliases are present and disagree, report an ambiguous input and refuse the affected controlled operation.

For asynchronous ZCode Agent calls, a launch result is not a completion event. For Codex, SubagentStop is not proof that its thread has closed. A controller-generated normalized `run_exit_confirmed` event requires separately verified terminal facts.

## Failure handling

An untrusted, skipped, malformed, timed-out, or unsupported hook does not prove a denial occurred. Record each failure distinctly. Probe ZCode `onError: "deny"` support and exact semantics before activating a template that uses that extension. In the reviewed implementation, an empty, non-object, or unparsable stdout result can be ignored when the process exits successfully, without triggering `onError`; successfully parsed JSON that violates the output schema follows a different error path. This option also does not cover every skip/cancel path. The controlled execution gateway must independently fail admission when the controller or required capability is unavailable.

## Primary references

- [Codex Hook reference](https://learn.chatgpt.com/docs/hooks)
- [ZCode Hook contracts at the reviewed commit](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/contracts/src/hooks/index.ts)
- [ZCode input aliases](https://github.com/zai-org/ZCode/blob/a60ba6a7e5a06742270ca605f5d2cc2766afa7e1/apps/zcode-cli/packages/core/src/hooks/configured-runner-input.ts)
