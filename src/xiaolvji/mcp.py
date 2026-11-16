"""Narrow, authenticated MCP 2025-11-25 stdio surface (Python standard library).

Only status, advisory queue/metrics, bounded events/committed-source reads, and authorized task
registration are exposed.
This module never grants permissions, launches workers, approves plans, submits
verification, or records review decisions. ``principal`` is authenticated once by
the process entry point; request parameters cannot substitute another authority.

The supported protocol is the initialization-based 2025-11-25 revision, not the
stateless 2026-07-28 revision. Modern dual-era clients can fall back after the
standard method-not-found response to their ``server/discover`` probe.

Primary specifications, inspected 2026-11-03:
https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
https://modelcontextprotocol.io/specification/2025-11-25/server/tools
https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import sys
import time
from typing import Any

from .contracts import Fault, Principal, canonical_json, load_json, read_json, validate_identifier
from . import __version__
from .repository_read import READ_ROLES, project_tree, read_file

PROTOCOL_VERSION = "2025-11-25"
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_TOOL_CALLS_PER_SECOND = 60
_READ_ROLES = READ_ROLES
_REGISTER_ROLES = {"operator", "planner", "coordinator"}
_TOOL_NAMES = {"parallel_status", "parallel_queue", "parallel_metrics", "parallel_events",
               "parallel_project_tree", "parallel_read_file", "parallel_register_task"}
_METHODS = {"initialize", "ping", "tools/list", "tools/call"}


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _object(value: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise _RpcError(-32602, "Invalid params")
    if "_meta" in value and not isinstance(value["_meta"], dict):
        raise _RpcError(-32602, "Invalid request metadata")
    return value


def _tool_arguments(value: dict, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if set(value) - allowed or required - set(value):
        raise Fault("INVALID_INPUT", "Tool arguments contain missing or unsupported fields.")
    return value


def _tool_result(value: Any, error: bool = False) -> dict:
    # Text JSON remains interoperable with clients that do not consume structured
    # tool output. It is escaped into one NDJSON frame by the outer serializer.
    content = canonical_json(value)
    if len(content.encode("utf-8")) > MAX_FRAME_BYTES // 2:
        raise Fault("RESULT_TOO_LARGE", "Result is too large; select a project or reduce the result limit.")
    return {"content": [{"type": "text", "text": content}], "isError": error}


def _tools(principal: Principal) -> list[dict]:
    project = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"}
    commit = {"type": "string", "pattern": "^(?:[0-9a-f]{40}|[0-9a-f]{64})$"}
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    tools = [
        {"name": "parallel_status", "description": "Read managed-worker occupancy, delivery states, and blockers. This never starts work.",
         "inputSchema": {"type": "object", "properties": {"project_id": project}, "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "parallel_queue",
         "description": "Read up to 200 advisory task rankings and admission blockers. Does not reserve capacity, start work, or grant execution or review authority.",
         "inputSchema": {"type": "object", "properties": {
             "project_id": project, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}},
             "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "parallel_metrics",
         "description": "Read bounded historical controller timing, occupancy, outcomes, and quality-record counts. Diagnostic data does not establish readiness, correctness, or review approval.",
         "inputSchema": {"type": "object", "properties": {"project_id": project}, "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "parallel_events", "description": "Read up to 1000 ordered controller events after a sequence cursor.",
         "inputSchema": {"type": "object", "properties": {
             "after": {"type": "integer", "minimum": 0, "default": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}},
             "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "parallel_project_tree",
         "description": "List one directory in an enabled project's committed Git snapshot under an approved plan. Returns an exact commit and bounded page; reuse that commit with next_cursor. Reads no untracked/worktree files and launches no worker.",
         "inputSchema": {"type": "object", "properties": {
             "project_id": project, "directory": {**path, "default": "."}, "commit": commit,
             "cursor": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}},
             "required": ["project_id"], "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "parallel_read_file",
         "description": "Read a bounded UTF-8 tracked regular file from an exact reachable commit in an enabled project with an approved plan. Never reads working files, symlink targets, gitlinks, arbitrary objects, or controller state.",
         "inputSchema": {"type": "object", "properties": {
             "project_id": project, "commit": commit, "path": path,
             "max_bytes": {"type": "integer", "minimum": 1, "maximum": 131072, "default": 65536}},
             "required": ["project_id", "commit", "path"], "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    ]
    if principal.role in _REGISTER_ROLES:
        task_schema = read_json(Path(__file__).parent / "resources" / "task.schema.json")
        tools.append({"name": "parallel_register_task",
                      "description": "Register one immutable task inside an already approved plan. Registration does not approve a plan or launch a worker. Reuse request_id for an identical retry.",
                      "inputSchema": {"type": "object", "properties": {
                          "project_id": project, "task": task_schema,
                          "request_id": {"type": "string", "minLength": 1, "maxLength": 200}},
                          "required": ["project_id", "task", "request_id"], "additionalProperties": False},
                      "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}})
    return tools


class _Session:
    def __init__(self, ledger, principal: Principal):
        if not isinstance(principal, Principal) or not principal.id or principal.role not in _READ_ROLES:
            raise Fault("UNAUTHORIZED", "MCP requires a pre-authenticated controller identity.")
        self.ledger, self.principal = ledger, principal
        self.initialized = False
        self.ready = False
        self.tool_times: deque[float] = deque()

    def notification(self, method: str, params: Any) -> None:
        # Notifications never produce a response or mutate the ledger. In
        # particular, a tools/call without an id cannot register a task.
        if method == "notifications/initialized" and self.initialized:
            if (isinstance(params, dict) and not (set(params) - {"_meta"})
                    and ("_meta" not in params or isinstance(params["_meta"], dict))):
                self.ready = True

    def request(self, method: str, params: Any) -> dict:
        if method not in _METHODS:
            raise _RpcError(-32601, "Method not found")
        if method == "ping":
            _object(params, {"_meta"})
            return {}
        if method == "initialize":
            if self.initialized:
                raise _RpcError(-32600, "Session was already initialized")
            params = _object(params, {"protocolVersion", "capabilities", "clientInfo", "_meta"},
                             {"protocolVersion", "capabilities", "clientInfo"})
            version = params["protocolVersion"]
            client = params["clientInfo"]
            if (not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version)
                    or not isinstance(params["capabilities"], dict) or not isinstance(client, dict)
                    or not all(isinstance(client.get(key), str) and 0 < len(client[key]) <= 256
                               for key in ("name", "version"))):
                raise _RpcError(-32602, "Malformed initialization parameters")
            self.initialized = True
            # A legacy server returns a version it actually implements when the
            # client asks for an unsupported revision; the client decides whether
            # to continue or disconnect, as the pinned specification requires.
            return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "xiaolvji", "version": __version__},
                    "instructions": "Read state and committed project snapshots, and register tasks only within an already approved plan. Capacity admission, execution, evidence, and final review remain controller operations."}
        if not self.ready:
            raise _RpcError(-32002, "Complete initialize and notifications/initialized before using tools")
        if method == "tools/list":
            params = _object(params, {"cursor", "_meta"})
            if "cursor" in params:
                raise _RpcError(-32602, "This bounded tool list has no continuation cursor")
            return {"tools": _tools(self.principal)}
        params = _object(params, {"name", "arguments", "_meta"}, {"name"})
        name, arguments = params["name"], params.get("arguments", {})
        if not isinstance(name, str) or name not in _TOOL_NAMES:
            raise _RpcError(-32602, "Unknown tool")
        if not isinstance(arguments, dict):
            raise _RpcError(-32602, "Tool arguments must be an object")
        try:
            now = time.monotonic()
            while self.tool_times and now - self.tool_times[0] >= 1:
                self.tool_times.popleft()
            if len(self.tool_times) >= MAX_TOOL_CALLS_PER_SECOND:
                raise Fault("RATE_LIMITED", "At most 60 tool calls per second are permitted on this connection.")
            self.tool_times.append(now)
            if name == "parallel_status":
                _tool_arguments(arguments, {"project_id"})
                project_id = arguments.get("project_id")
                if "project_id" in arguments:
                    validate_identifier(project_id, "project_id")
                return _tool_result(self.ledger.status(project_id))
            if name == "parallel_queue":
                _tool_arguments(arguments, {"project_id", "limit"})
                project_id, limit = arguments.get("project_id"), arguments.get("limit", 100)
                if "project_id" in arguments:
                    validate_identifier(project_id, "project_id")
                if type(limit) is not int or not 1 <= limit <= 200:
                    raise Fault("INVALID_INPUT", "Queue limit must be an integer between 1 and 200.")
                return _tool_result(self.ledger.queue(project_id, limit))
            if name == "parallel_metrics":
                _tool_arguments(arguments, {"project_id"})
                project_id = arguments.get("project_id")
                if "project_id" in arguments:
                    validate_identifier(project_id, "project_id")
                from .metrics import runtime_metrics
                return _tool_result(runtime_metrics(self.ledger, project_id))
            if name == "parallel_events":
                _tool_arguments(arguments, {"after", "limit"})
                after, limit = arguments.get("after", 0), arguments.get("limit", 100)
                if type(after) is not int or not 0 <= after <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 1000:
                    raise Fault("INVALID_INPUT", "Event cursor must be nonnegative and limit between 1 and 1000.")
                events = self.ledger.events(after, limit)
                return _tool_result({"events": events, "next_cursor": events[-1]["seq"] if events else after})
            if name == "parallel_project_tree":
                _tool_arguments(arguments, {"project_id", "directory", "commit", "cursor", "limit"}, {"project_id"})
                if "commit" in arguments and not isinstance(arguments["commit"], str):
                    raise Fault("INVALID_INPUT", "An explicit commit must be a full Git commit ID.")
                return _tool_result(project_tree(self.ledger, self.principal, **arguments))
            if name == "parallel_read_file":
                _tool_arguments(arguments, {"project_id", "commit", "path", "max_bytes"}, {"project_id", "commit", "path"})
                return _tool_result(read_file(self.ledger, self.principal, **arguments))
            if self.principal.role not in _REGISTER_ROLES:
                raise Fault("UNAUTHORIZED", "Task registration requires an authenticated planner or coordinator.")
            _tool_arguments(arguments, {"project_id", "task", "request_id"}, {"project_id", "task", "request_id"})
            validate_identifier(arguments["project_id"], "project_id")
            return _tool_result(self.ledger.register_task(arguments["project_id"], arguments["task"],
                                                         self.principal, arguments["request_id"]))
        except Fault as exc:
            return _tool_result({"error": exc.as_dict()}, error=True)
        except Exception:
            # Exception messages may contain credentials or command strings.
            return _tool_result({"error": {"code": "INTERNAL_ERROR", "message": "Controller operation failed."}}, error=True)


def _response_error(identifier: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": identifier, "error": error}


def _write(stream, frame: dict) -> None:
    wire = canonical_json(frame) + "\n"
    if len(wire.encode("utf-8")) > MAX_FRAME_BYTES:
        wire = canonical_json(_response_error(frame.get("id"), -32603, "Response exceeds transport size limit")) + "\n"
    try:
        stream.write(wire)
    except TypeError:  # A caller may explicitly supply a binary stream.
        stream.write(wire.encode("utf-8"))
    stream.flush()


def serve(ledger, principal: Principal, instream=None, outstream=None) -> int:
    """Serve bounded newline JSON-RPC until EOF; return 0 or transport failure 1.

    The CLI authenticates the capability before calling this function. Stdout is
    reserved for protocol frames. Oversized input terminates the connection after
    one error rather than draining an unbounded frame. Ordinary malformed frames
    receive bounded errors and may be followed by a valid request.
    """
    session = _Session(ledger, principal)
    instream = getattr(sys.stdin, "buffer", sys.stdin) if instream is None else instream
    outstream = sys.stdout if outstream is None else outstream
    malformed = 0
    try:
        while True:
            raw = instream.readline(MAX_FRAME_BYTES + 1)
            if raw in (b"", ""):
                return 0
            size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            if size > MAX_FRAME_BYTES:
                _write(outstream, _response_error(None, -32600, "Input frame exceeds two MiB", {"code": "INPUT_TOO_LARGE"}))
                return 1
            try:
                request = load_json(raw, MAX_FRAME_BYTES)
            except Fault:
                _write(outstream, _response_error(None, -32700, "Parse error"))
                malformed += 1
                if malformed >= 16:
                    return 1
                continue
            identifier = request.get("id") if isinstance(request, dict) else None
            valid_id = type(identifier) is int or (isinstance(identifier, str) and 0 < len(identifier) <= 200)
            if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                    or set(request) - {"jsonrpc", "id", "method", "params"}
                    or not isinstance(request.get("method"), str) or not request["method"]
                    or ("id" in request and not valid_id)):
                _write(outstream, _response_error(identifier if valid_id else None, -32600, "Invalid request"))
                malformed += 1
                if malformed >= 16:
                    return 1
                continue
            method, params = request["method"], request.get("params", {})
            if "id" not in request:
                session.notification(method, params)
                continue
            try:
                result = session.request(method, params)
                response = {"jsonrpc": "2.0", "id": identifier, "result": result}
            except _RpcError as exc:
                response = _response_error(identifier, exc.code, exc.message, exc.data)
            except Exception:
                response = _response_error(identifier, -32603, "Internal error")
            _write(outstream, response)
    except (BrokenPipeError, OSError, UnicodeError):
        return 1
