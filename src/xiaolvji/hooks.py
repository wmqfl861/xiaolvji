"""Small synchronous hook policy. All authority comes from trusted context.

A hook event is an observation, never an admission, process exit, or approval.
This module intentionally has no database mutation or process launch operation.
"""
from __future__ import annotations

import json
from typing import Any

from .contracts import Fault

CODEX_EVENTS = frozenset({"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop", "SubagentStop", "Interrupt"})
ZCODE_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "PermissionRequest"})
SPAWN_TOOLS = frozenset({
    "agent", "task", "spawn_agent", "followup_task", "resume_agent", "send_input",
    "workflow", "createworkflow", "amendworkflow", "resumeworkflowrun", "sendmessage",
})
COORDINATOR_MCP_TOOLS = frozenset({
    "parallel_status", "parallel_queue", "parallel_metrics", "parallel_events", "parallel_project_tree",
    "parallel_read_file", "parallel_register_task",
})
# Exact reserved server IDs for explicit MCP wiring. Codex's current native
# sanitizer converts '-' to '_' before building hook_tool_name_for. The
# fully loaded plugin hook path still requires host-specific acceptance.
# https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/mcp/mod.rs
# https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/mcp.rs
# The ZCode plugin loader
# adds plugin:<manifest-name>:<server-key>; its descriptor replaces ':' with
# '_' while preserving '-'. No suffix matching or caller-provided namespace.
COORDINATOR_MCP_PREFIXES = {
    "codex": ("mcp__parallel_development__",),
    "zcode": ("mcp__parallel-development__", "mcp__parallel_development__",
              "mcp__plugin_parallel-development_parallel-development__"),
}
ALIASES = {
    "event": ("hook_event_name", "hookEventName"),
    "session_id": ("session_id", "sessionId"),
    "tool_name": ("tool_name", "toolName"),
    "tool_use_id": ("tool_use_id", "toolCallId"),
    "tool_input": ("tool_input", "toolInput"),
    "stop_hook_active": ("stop_hook_active", "stopHookActive"),
}
POLICY_TEXT = (
    "Apply the Parallel Development Policy for this opted-in project. Register explicit task contracts, "
    "dependency gates, write scopes, and acceptance criteria. Admit compatible work only through the "
    "authoritative controller ledger. Process results individually and refill capacity after confirmed "
    "process-tree exit and reservation release. Look for useful independent work inside the approved "
    "scope at task and contract milestones; do not create work just to occupy slots. Do not treat a "
    "model completion message or a native hook as process termination or final acceptance. Preserve "
    "existing tests, model settings, and the designated independent review. Respect cancellation. "
    "Plugin instructions are in English; answer the user in their preferred language."
)


def denial(reason: str) -> dict:
    """Event-specific deny; never an unconditional allow or permission grant."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason[:1200]}}


def _coordinator_mcp_tool(adapter: str, name: str) -> bool:
    return any(name == prefix + tool for prefix in COORDINATOR_MCP_PREFIXES[adapter]
               for tool in COORDINATOR_MCP_TOOLS)


def normalize_input(adapter: str, payload: Any) -> dict:
    if adapter not in ("codex", "zcode"):
        raise Fault("CAPABILITY_UNSUPPORTED", "Unsupported hook adapter.")
    if not isinstance(payload, dict):
        raise Fault("INVALID_HOOK_INPUT", "Hook input must be a JSON object.")
    try:
        if len(json.dumps(payload, ensure_ascii=True, allow_nan=False)) > 1_048_576:
            raise Fault("INVALID_HOOK_INPUT", "Hook input exceeds the one MiB limit.")
    except (TypeError, ValueError, RecursionError) as exc:
        raise Fault("INVALID_HOOK_INPUT", "Hook input must contain finite JSON values.") from exc
    normalized: dict[str, Any] = {}
    for name, aliases in ALIASES.items():
        supplied = [payload[key] for key in aliases if key in payload]
        if supplied and any(json.dumps(value, sort_keys=True, ensure_ascii=True) != json.dumps(supplied[0], sort_keys=True, ensure_ascii=True) for value in supplied[1:]):
            raise Fault("AMBIGUOUS_HOOK_INPUT", "Conflicting hook input aliases cannot identify a controlled operation.")
        if supplied:
            normalized[name] = supplied[0]
    if not isinstance(normalized.get("event"), str):
        raise Fault("INVALID_HOOK_INPUT", "Hook input requires an event name.")
    for field in ("session_id", "tool_name", "tool_use_id"):
        if field in normalized and (not isinstance(normalized[field], str) or not 0 < len(normalized[field]) <= 512):
            raise Fault("INVALID_HOOK_INPUT", "Hook identity fields must be bounded nonempty strings.")
    if "stop_hook_active" in normalized and type(normalized["stop_hook_active"]) is not bool:
        raise Fault("INVALID_HOOK_INPUT", "Stop continuation state must be a boolean.")
    if "cwd" in payload:
        if not isinstance(payload["cwd"], str) or not payload["cwd"] or len(payload["cwd"]) > 16_384 or "\x00" in payload["cwd"]:
            raise Fault("INVALID_HOOK_INPUT", "Hook working directory is invalid.")
        normalized["cwd"] = payload["cwd"]
    if normalized["event"] == "PreToolUse" and not normalized.get("tool_name"):
        raise Fault("INVALID_HOOK_INPUT", "PreToolUse requires a tool name.")
    return normalized


def _count(context: dict, name: str, default: int = 0) -> int:
    value = context.get(name, default)
    return value if type(value) is int and 0 <= value <= 1_000_000 else default


def _context_text(context: dict) -> str:
    status = {key: context.get(key) for key in ("policy_version", "project_id", "profile", "plan_revision")}
    status.update({key: _count(context, key) for key in ("occupied", "capacity", "ready_count")})
    blockers = context.get("blockers", [])
    status["blockers"] = [str(item)[:160] for item in blockers[:8]] if isinstance(blockers, list) else []
    return POLICY_TEXT + "\nController status: " + json.dumps(status, ensure_ascii=True, separators=(",", ":"))[:1800]


def handle(adapter: str, payload: dict, context: dict | None = None) -> dict:
    """Return a host-correct response using controller-constructed context only.

    `context` must never be copied from hook input or accepted as a CLI JSON flag.
    Matching a host parent session ID does not establish a worker identity.
    Stop continuation is disabled unless a durable reservation was already made
    by the controller; this pure handler cannot increment the reservation count.
    """
    context = context if isinstance(context, dict) else {}
    normalized = normalize_input(adapter, payload)
    event = normalized["event"]
    supported = CODEX_EVENTS if adapter == "codex" else ZCODE_EVENTS
    if event not in supported or context.get("enrolled") is not True:
        return {}
    if event in ("SessionStart", "SubagentStart", "UserPromptSubmit"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": _context_text(context)}}
    if event == "PreToolUse":
        tool = normalized["tool_name"].rsplit("__", 1)[-1].rsplit(".", 1)[-1].casefold()
        managed_queue = context.get("controller_owns_queue") is True or context.get("profile") == "controlled_executor"
        if tool in SPAWN_TOOLS and (managed_queue or context.get("actor_role") == "worker"):
            return denial("This project's controller owns worker admission. Register eligible work with parallel-dev and let the rolling dispatcher launch it; native spawning or resuming would create an unmanaged worker.")
        if context.get("cancelled") is True and context.get("actor_role") == "worker":
            return denial("This task has been cancelled. Do not start more work; return the current handover to the controller.")
        if context.get("profile") == "controlled_executor" and context.get("identity_verified") is not True:
            if (context.get("actor_role") != "worker"
                    and _coordinator_mcp_tool(adapter, normalized["tool_name"])):
                # This is a coordinator RPC, not a worker operation. Returning
                # {} leaves native permission/trust checks intact. The MCP
                # process authenticates its principal and enforces each tool's
                # authority; hook names never grant a capability or a slot.
                return {}
            return denial("The controller could not bind this operation to an authenticated actor and current attempt. A host session ID alone is not a worker identity. Reconcile through the execution gateway before continuing.")
        return {}
    if event == "Stop":
        count = _count(context, "stop_continuations", 2)
        runnable = _count(context, "ready_count") > 0 and _count(context, "occupied") < _count(context, "capacity")
        if (context.get("continuation_reserved") is True and count < 2 and runnable
                and context.get("cancelled") is not True and context.get("paused") is not True
                and context.get("profile") != "blocked"):
            return {"decision": "block", "reason": "Authorized executable work remains and a bounded continuation was reserved. Reconcile completed handovers and dispatch eligible tasks through the controller. Respect cancellation and the continuation budget."}
    # PostToolUse, SubagentStop, Interrupt and PermissionRequest do not release
    # slots, launch tasks, retry, continue cancellation, or grant permissions.
    return {}
