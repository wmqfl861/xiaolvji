"""Versioned local CLI bridges; streamed model reports never release a slot."""
from __future__ import annotations

import os
from pathlib import Path

from .contracts import Fault, load_json

MAX_EVENT_BYTES = 1_048_576
MAX_PROMPT_BYTES = 1_048_576
ZCODE_SOURCE_COMMIT = "a60ba6a7e5a06742270ca605f5d2cc2766afa7e1"
ZCODE_DISALLOWED_TOOLS = (
    "Agent", "Task", "Workflow", "CreateWorkflow", "AmendWorkflow",
    "ResumeWorkflowRun", "SendMessage",
)


def _engine(engine: str) -> str:
    if engine not in ("codex", "zcode"):
        raise Fault("CAPABILITY_UNSUPPORTED", "Only local Codex and ZCode CLI adapters are available.")
    return engine


def build_command(engine: str, workspace: str | Path | dict, prompt: str,
                  executable: str | None = None) -> dict:
    """Build argv without a shell or model/reasoning overrides.

    This does not establish OS isolation or validate authentication. The gateway
    must perform those checks and provide the intended trusted configuration
    before launch. A private HOME does not itself preserve host model settings.
    ZCode receives the prompt in argv because the pinned CLI has no verified
    stdin or prompt-file interface.
    """
    _engine(engine)
    if isinstance(workspace, dict):
        workspace = workspace.get("path")
    if not isinstance(workspace, (str, Path)):
        raise Fault("INVALID_INPUT", "A workspace path is required.")
    path = Path(workspace)
    if not path.is_absolute() or not path.is_dir():
        raise Fault("INVALID_INPUT", "The worker workspace must be an existing absolute directory.")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise Fault("INVALID_INPUT", "The task prompt must be nonempty text without NUL characters.")
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Fault("INVALID_INPUT", "The prompt must be valid Unicode text.") from exc
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise Fault("INVALID_INPUT", "The task prompt exceeds the one MiB input limit.")
    executable = executable or engine
    if not isinstance(executable, str) or not executable or "\x00" in executable or executable.startswith("-"):
        raise Fault("INVALID_INPUT", "The host executable must be one command name or absolute path.")
    cwd = str(path.resolve())
    if engine == "codex":
        # This is a documented native-tool setting, not an OS process boundary.
        # Successful config loading alone does not prove runtime enforcement.
        return {
            "argv": [executable, "exec", "--json", "--sandbox", "workspace-write",
                     "--cd", cwd, "-c", "agents.enabled=false", "-"],
            "stdin_text": prompt,
            "env": {},
        }
    # A conservative limit below common Linux MAX_ARG_STRLEN and Windows command
    # line limits. Fail explicitly; never truncate an approved task description.
    if len(prompt_bytes) > 120_000 or (os.name == "nt" and len(prompt) + len(cwd) + len(executable) > 28_000):
        raise Fault("CAPABILITY_UNSUPPORTED", "ZCode's verified argv prompt interface cannot safely carry this prompt; shorten the task contract.")
    return {
        # ZCode extracts disallowed-tool flags before parsing global options.
        # Keep the prompt attached to its option so leading dashes remain data
        # for both native parsing stages instead of becoming another CLI flag.
        "argv": [executable, "--cwd", cwd, "--prompt=" + prompt,
                 "--output-format", "stream-json", "--workflow-mode", "disabled",
                 "--disallowed-tools", ",".join(ZCODE_DISALLOWED_TOOLS), "--mode", "guarded"],
        "stdin_text": None,
        "env": {},
    }


def parse_event(engine: str, line: str | bytes) -> dict | None:
    """Normalize one bounded host NDJSON line; preserve no process-exit authority."""
    _engine(engine)
    if not isinstance(line, (str, bytes)):
        raise Fault("INVALID_EVENT", "A host event must be a JSON line.")
    if isinstance(line, bytes):
        if len(line) > MAX_EVENT_BYTES:
            raise Fault("INVALID_EVENT", "Host event exceeds the one MiB limit.")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Fault("INVALID_EVENT", "Host event is not UTF-8.") from exc
    try:
        size = len(line.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise Fault("INVALID_EVENT", "Host event is not valid Unicode.") from exc
    if size > MAX_EVENT_BYTES:
        raise Fault("INVALID_EVENT", "Host event exceeds the one MiB limit.")
    if not line.strip():
        return None
    try:
        # The shared loader also rejects exponent overflow, excessive nesting
        # and escaped unpaired surrogates anywhere in the event payload.
        value = load_json(line, limit=MAX_EVENT_BYTES)
    except Fault:
        # Loader diagnostics can contain an untrusted duplicate key. Do not
        # expose it through either the public fault or an exception traceback.
        raise Fault("INVALID_EVENT", "Host output is not one unambiguous JSON object.") from None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not 0 < len(value["type"]) <= 128:
        raise Fault("INVALID_EVENT", "Host event requires a bounded type field.")
    host_type = value["type"]
    session_id = value.get("thread_id") if engine == "codex" else value.get("sessionId")
    if session_id is not None and (not isinstance(session_id, str) or not 0 < len(session_id) <= 256):
        raise Fault("INVALID_EVENT", "Host session identity has an invalid shape.")
    terminal_report = host_type in (("turn.completed", "turn.failed") if engine == "codex" else ("result",))
    return {
        "engine": engine,
        "host_type": host_type,
        "session_id": session_id,
        "kind": "turn_reported" if terminal_report else "host_event",
        "terminal_report": terminal_report,
        "process_tree_terminated": False,
        "payload": value,
    }
