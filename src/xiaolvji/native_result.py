"""Bounded native CLI result validation; never process or approval authority.

The executor must already prove process-tree exit before the pipeline calls this
module. A host's completed turn is an additional semantic gate, not proof that
the task met its acceptance criteria. The returned digest binds the exact
durable stdout bytes that were inspected; raw model text is never copied into
the result or into exception diagnostics.

This contract covers one implementation prompt, with native delegation and
workflow continuation disabled. ZCode's pinned CLI writes a final ``result``
and returns zero even when ``projection.status`` is ``error``. Its mapped event
stream must therefore be checked as well as its process exit status. See pinned
ZCode ``packages/cli/src/prompt-command.ts``, ``headless-workflow.ts``, and
``packages/bootstrap/src/zcode-protocol/session-mapper.ts``.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .adapters import MAX_EVENT_BYTES, parse_event
from .contracts import Fault, bytes_digest, load_json


# Matches the supervisor's captured stdout budget. A truncated success cannot
# be recovered by treating a partial prefix or arbitrary tail as a complete run.
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 65_536


def _invalid(message: str):
    raise Fault("NATIVE_RESULT_INVALID", message)


def _failed(message: str):
    raise Fault("NATIVE_TURN_FAILED", message)


def _identifier(value) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        _invalid("Native lifecycle identity is missing or invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid("Native lifecycle identity is missing or invalid.")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        _invalid("Native lifecycle identity is missing or invalid.")
    return value


def _stdout_bytes(stdout_path) -> bytes:
    try:
        path = Path(stdout_path)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            _invalid("Native stdout must be an absolute, non-symlink regular file.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                _invalid("Native stdout must be an absolute, non-symlink regular file.")
            if before.st_size > MAX_STDOUT_BYTES:
                _invalid("Native stdout exceeds the complete-log validation budget.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_STDOUT_BYTES + 1)
            after = os.fstat(descriptor)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after) or len(data) != before.st_size:
                _invalid("Native stdout changed during validation.")
            if len(data) > MAX_STDOUT_BYTES:
                _invalid("Native stdout exceeds the complete-log validation budget.")
            return data
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError, RuntimeError):
        # No raw path, provider error, file bytes or OS diagnostics are returned.
        _invalid("Native stdout is unavailable or is not a stable regular file.")


def verify_host_output(engine: str, stdout_path: str | Path, *,
                       truncated: bool = False,
                       expected_session_id: str | None = None) -> dict:
    """Validate a complete, successful single-prompt CLI stdout transcript.

    Call only with the executor-owned durable stdout path and truncation flag,
    never a task-supplied path. Verification and fixture commands are not native
    CLI transcripts. Recovery must call this function again on the same durable
    executor evidence before treating a native implementation as successful.

    Unknown bounded nonterminal events remain observable. A normal tool command
    may fail and be corrected during a turn. An explicit native error, failed
    turn, duplicate/ambiguous lifecycle, truncated log, or missing final report
    cannot become success merely because the host exited with code zero.
    """
    if engine not in ("codex", "zcode"):
        raise Fault("CAPABILITY_UNSUPPORTED", "Native result validation supports only Codex and ZCode.")
    if type(truncated) is not bool:
        _invalid("Native stdout truncation state must be explicit Boolean data.")
    if truncated:
        _invalid("Native stdout was truncated; the complete native result cannot be verified.")
    if expected_session_id is not None:
        expected_session_id = _identifier(expected_session_id)
    data = _stdout_bytes(stdout_path)
    session_id = None
    turn_id = None
    thread_started = False
    started = False
    completed = False
    terminal = False
    count = 0

    for line in data.split(b"\n"):
        if len(line) > MAX_EVENT_BYTES:
            _invalid("Native stdout contains an oversized event.")
        if not line.strip():
            continue
        count += 1
        if count > MAX_EVENTS:
            _invalid("Native stdout exceeds the event-count validation budget.")
        try:
            # Also reject excessive nesting and unpaired escaped surrogates in
            # arbitrary event payloads, not only the lifecycle identity fields.
            load_json(line, limit=MAX_EVENT_BYTES)
            parsed = parse_event(engine, line)
        except Fault:
            _invalid("Native stdout is not a valid bounded JSON event stream.")
        if parsed is None:  # Only blank lines can produce None; handled above.
            _invalid("Native stdout contains an unrecognized empty event.")
        if terminal:
            _invalid("Native stdout contains events after the final result.")
        event = parsed["payload"]
        kind = parsed["host_type"]
        observed_session = parsed["session_id"]
        if observed_session is not None:
            observed_session = _identifier(observed_session)
            if session_id is None:
                session_id = observed_session
            elif observed_session != session_id:
                _invalid("Native stdout mixes different session identities.")
            if expected_session_id is not None and session_id != expected_session_id:
                _invalid("Native stdout does not match the expected session identity.")

        if kind in {"error", "turn.failed"}:
            _failed("The native host reported a failed turn or an execution error.")

        if engine == "codex":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "error":
                # The actual installed CLI reports model-metadata fallback as
                # an error item even when it later emits turn.completed. Do not
                # silently accept changed model-capability/quality assumptions.
                _failed("The native host reported an error item; its successful configuration is unverified.")
            if kind == "thread.started":
                if observed_session is None or thread_started or started or completed or count != 1:
                    _invalid("Codex must start one identified thread before its implementation turn.")
                thread_started = True
            elif kind == "turn.started":
                if not thread_started or started or completed:
                    _invalid("Codex has an ambiguous or missing turn start.")
                started = True
            elif kind == "turn.completed":
                if not started or completed:
                    _invalid("Codex completed a turn without one matching start.")
                if event.get("error") is not None:
                    _failed("The native host attached an error to its completed turn.")
                completed = terminal = True
        else:
            if kind in {"turn.started", "turn.completed", "result"}:
                if observed_session is None:
                    _invalid("ZCode lifecycle events require a session identity.")
                observed_turn = _identifier(event.get("turnId"))
                if turn_id is not None and observed_turn != turn_id:
                    _invalid("ZCode stdout mixes different implementation turns.")
                if kind == "turn.started":
                    if started or completed:
                        _invalid("ZCode has an ambiguous implementation turn start.")
                    turn_id, started = observed_turn, True
                elif kind == "turn.completed":
                    if not started or completed:
                        _invalid("ZCode completed a turn without one matching start.")
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or "resultType" not in payload:
                        _invalid("ZCode's completed turn is missing its result type.")
                    # ZCode uses TurnComplete even for cancellation and budget
                    # exhaustion; the reducer still sets the projection idle.
                    if payload["resultType"] != "success":
                        _failed("ZCode's completed turn reports cancellation or unsuccessful execution.")
                    completed = True
                else:
                    if not started or not completed:
                        _invalid("ZCode result lacks a complete implementation turn.")
                    projection = event.get("projection")
                    if not isinstance(projection, dict):
                        _invalid("ZCode result is missing its final projection.")
                    if projection.get("status") not in ("idle", "completed"):
                        _failed("ZCode's final projection does not report a successful completed turn.")
                    if not isinstance(event.get("response"), str):
                        _invalid("ZCode result is missing its response field.")
                    if (type(event.get("eventCount")) is not int or
                            not 0 <= event["eventCount"] <= MAX_EVENTS):
                        _invalid("ZCode result has an invalid event count.")
                    terminal = True
            elif event.get("turnId") is not None and turn_id is not None:
                if _identifier(event["turnId"]) != turn_id:
                    _invalid("ZCode stdout mixes different implementation turns.")

    if not terminal or not completed or session_id is None:
        _invalid("Native stdout is missing a complete successful final result.")
    return {"schema_version": 1, "engine": engine, "status": "completed",
            "session_id": session_id, "turn_id": turn_id, "event_count": count,
            "terminal_type": "turn.completed" if engine == "codex" else "result",
            "stdout_digest": bytes_digest(data), "stdout_bytes": len(data),
            "stdout_truncated": False, "process_tree_terminated": False}
