"""One bounded Codex app-server turn inside an attested external sandbox.

This module is copied as immutable controller code into the private HOME and
executed with Python -I -S. It is deliberately stdlib-only. It is NOT an OS
sandbox: callers must use the controlled executor and its verified namespace,
mount, seccomp, and process-tree boundary. No approval is ever accepted here.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time
import tomllib


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_MESSAGES = 100000
PROVIDER = "xiaolvji_bridge"
RUNNER_HOME_PATH = ".xiaolvji/codex_app_server.py"
PRIVATE_HOME = "/home/xiaolvji"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,254}\Z", re.ASCII)
_QUALITY_KEYS = {"model_reasoning_effort", "model_reasoning_summary", "model_verbosity", "service_tier",
                 "model_context_window", "model_auto_compact_token_limit"}
_CONFIG_KEYS = {
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary", "model_verbosity",
    "service_tier", "model_context_window", "model_auto_compact_token_limit", "web_search", "model_catalog_json",
    *("model_providers." + PROVIDER + "." + key for key in
      ("name", "base_url", "env_key", "wire_api", "requires_openai_auth", "supports_websockets")),
}


class HostError(Exception):
    """A bounded, non-secret protocol failure code."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError("nonfinite")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 48:
                raise ValueError("depth")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((entry, depth + 1) for entry in item.values())
            elif isinstance(item, list):
                pending.extend((entry, depth + 1) for entry in item)
            elif isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HostError("NATIVE_PROTOCOL_INVALID_JSON") from None


def _absolute(value):
    return (isinstance(value, str) and value.startswith("/") and len(value) <= 4096
            and not any(ord(char) < 32 for char in value)
            and all(part not in {"", ".", ".."} for part in value.split("/")[1:]))


def validate_input(value):
    required = {"version", "executable", "cwd", "prompt", "model", "effort", "argv_extra"}
    allowed = required | {"timeout_seconds"}
    if (not isinstance(value, dict) or required - set(value) or set(value) - allowed
            or type(value["version"]) is not int or value["version"] != 1):
        raise HostError("NATIVE_INPUT_INVALID")
    if not _absolute(value["executable"]) or not _absolute(value["cwd"]):
        raise HostError("NATIVE_INPUT_INVALID")
    if (not isinstance(value["prompt"], str) or not value["prompt"].strip()
            or "\x00" in value["prompt"] or len(value["prompt"].encode("utf-8")) > MAX_PROMPT_BYTES
            or not isinstance(value["model"], str) or not _MODEL.fullmatch(value["model"])
            or not isinstance(value["effort"], str) or not _TOKEN.fullmatch(value["effort"])):
        raise HostError("NATIVE_INPUT_INVALID")
    argv = value["argv_extra"]
    if (not isinstance(argv, list) or len(argv) > 64 or len(argv) % 2
            or any(not isinstance(item, str) or "\x00" in item or len(item) > 8192 for item in argv)):
        raise HostError("NATIVE_CONFIG_INVALID")
    settings = {}
    for flag, setting in zip(argv[::2], argv[1::2]):
        key, separator, raw = setting.partition("=")
        if flag != "-c" or not separator or key not in _CONFIG_KEYS or key in settings:
            raise HostError("NATIVE_CONFIG_INVALID")
        try:
            parsed = tomllib.loads("value=" + raw)
            if set(parsed) != {"value"}:
                raise ValueError("multiple settings")
            settings[key] = parsed["value"]
        except (ValueError, KeyError):
            raise HostError("NATIVE_CONFIG_INVALID") from None
    required_settings = {
        "model": value["model"], "model_provider": PROVIDER,
        "model_reasoning_effort": value["effort"], "web_search": "disabled",
        "model_providers." + PROVIDER + ".base_url": "http://127.0.0.1:38119/v1",
        "model_providers." + PROVIDER + ".env_key": "XIAOLVJI_PROVIDER_CAPABILITY",
        "model_providers." + PROVIDER + ".wire_api": "responses",
        "model_providers." + PROVIDER + ".requires_openai_auth": False,
        "model_providers." + PROVIDER + ".supports_websockets": False,
    }
    if any(type(settings.get(key)) is not type(expected) or settings.get(key) != expected
           for key, expected in required_settings.items()):
        raise HostError("NATIVE_CONFIG_MISMATCH")
    if ("model_catalog_json" in settings
            and settings["model_catalog_json"] != PRIVATE_HOME + "/.codex/model-catalog.json"):
        raise HostError("NATIVE_CONFIG_INVALID")
    timeout = value.get("timeout_seconds", 3600)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 86400 or not math.isfinite(timeout):
        raise HostError("NATIVE_INPUT_INVALID")
    return dict(value, timeout_seconds=timeout)


class _Messages:
    """Bound bytes before JSON parsing, including unterminated output lines."""
    def __init__(self, stream):
        self.fd = stream.fileno()
        self.buffer = bytearray()

    def read(self, deadline):
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                if not line or len(line) > MAX_MESSAGE_BYTES:
                    raise HostError("NATIVE_PROTOCOL_INVALID_JSON")
                value = _json(line)
                if not isinstance(value, dict):
                    raise HostError("NATIVE_PROTOCOL_INVALID_JSON")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostError("NATIVE_PROTOCOL_TIMEOUT")
            ready, _, _ = select.select([self.fd], [], [], min(remaining, .25))
            if not ready:
                continue
            chunk = os.read(self.fd, min(65536, MAX_MESSAGE_BYTES + 1 - len(self.buffer)))
            if not chunk:
                raise HostError("NATIVE_PROTOCOL_TRUNCATED")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_MESSAGE_BYTES and b"\n" not in self.buffer:
                raise HostError("NATIVE_PROTOCOL_MESSAGE_TOO_LARGE")


def _emit(output, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HostError("NATIVE_PROTOCOL_MESSAGE_TOO_LARGE")
    output.write(raw + "\n")
    output.flush()


class Client:
    """Strict protocol sequencing; native notifications never prove tree exit."""
    def __init__(self, configuration, output):
        self.configuration = validate_input(configuration)
        self.expected_quality = {}
        for setting in self.configuration["argv_extra"][1::2]:
            key, _, raw = setting.partition("=")
            if key in _QUALITY_KEYS:
                self.expected_quality[key] = tomllib.loads("value=" + raw)["value"]
        self.output = output
        self.thread_id = None
        self.turn_id = None
        self.started = False
        self.settings_verified = False
        self.completed = False
        self.approvals_denied = 0
        self.pages = 0
        self.model_found = False
        self.seen_cursors = set()
        self.pending = {}
        self.next_id = 0
        self.process = None
        self.model_usage = None

    def send(self, method, params):
        self.next_id += 1
        self.pending[self.next_id] = method
        self.write({"id": self.next_id, "method": method, "params": params})

    def write(self, message):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(raw) > MAX_INPUT_BYTES:
            raise HostError("NATIVE_INPUT_INVALID")
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def _same_thread(self, params, *, turn=False):
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            raise HostError("NATIVE_IDENTITY_MISMATCH")
        if turn and params.get("turnId") != self.turn_id:
            raise HostError("NATIVE_IDENTITY_MISMATCH")

    def _turn(self, item):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not 0 < len(item["id"]) <= 256:
            raise HostError("NATIVE_IDENTITY_MISMATCH")
        if self.turn_id is None:
            self.turn_id = item["id"]
        elif self.turn_id != item["id"]:
            raise HostError("NATIVE_IDENTITY_MISMATCH")

    def result(self, method, result):
        cfg = self.configuration
        if not isinstance(result, dict):
            raise HostError("NATIVE_PROTOCOL_INVALID_RESULT")
        if method == "initialize":
            self.write({"method": "initialized", "params": {}})
            self.send("configRequirements/read", {})
        elif method == "configRequirements/read":
            if "requirements" not in result or result["requirements"] is not None:
                raise HostError("NATIVE_MANAGED_REQUIREMENTS_UNSUPPORTED")
            self.send("config/read", {"cwd": cfg["cwd"], "includeLayers": False})
        elif method == "config/read":
            config = result.get("config")
            if (not isinstance(config, dict) or config.get("model") != cfg["model"]
                    or config.get("model_provider") != PROVIDER
                    or config.get("model_reasoning_effort") != cfg["effort"]
                    or config.get("web_search") != "disabled"
                    or any(type(config.get(key)) is not type(expected) or config.get(key) != expected
                           for key, expected in self.expected_quality.items())
                    or config.get("approval_policy") not in (None, "on-request")
                    or config.get("approvals_reviewer") not in (None, "user")):
                raise HostError("NATIVE_EFFECTIVE_CONFIG_MISMATCH")
            self.send("model/list", {"includeHidden": True, "limit": 100})
        elif method == "model/list":
            self.pages += 1
            entries = result.get("data")
            if self.pages > 16 or not isinstance(entries, list) or len(entries) > 100:
                raise HostError("NATIVE_MODEL_CATALOG_INVALID")
            for item in entries:
                if not isinstance(item, dict):
                    raise HostError("NATIVE_MODEL_CATALOG_INVALID")
                if item.get("model") != cfg["model"]:
                    continue
                if self.model_found:
                    raise HostError("NATIVE_MODEL_CATALOG_INVALID")
                levels = item.get("supportedReasoningEfforts")
                if (not isinstance(levels, list)
                        or not any(isinstance(level, dict) and level.get("reasoningEffort") == cfg["effort"] for level in levels)):
                    raise HostError("NATIVE_REASONING_EFFORT_UNSUPPORTED")
                self.model_found = True
            cursor = result.get("nextCursor")
            if cursor is not None:
                if not isinstance(cursor, str) or not 0 < len(cursor) <= 4096 or cursor in self.seen_cursors:
                    raise HostError("NATIVE_MODEL_CATALOG_INVALID")
                self.seen_cursors.add(cursor)
                self.send("model/list", {"includeHidden": True, "limit": 100, "cursor": cursor})
            elif not self.model_found:
                raise HostError("NATIVE_MODEL_UNSUPPORTED")
            else:
                self.send("thread/start", {"cwd": cfg["cwd"], "model": cfg["model"], "modelProvider": PROVIDER,
                                           "sandbox": "workspace-write", "ephemeral": True})
        elif method == "thread/start":
            thread = result.get("thread")
            if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
                    or not 0 < len(thread["id"]) <= 256 or self.thread_id is not None
                    or result.get("model") != cfg["model"] or result.get("modelProvider") != PROVIDER
                    or result.get("reasoningEffort") != cfg["effort"] or result.get("cwd") != cfg["cwd"]
                    or result.get("approvalPolicy") != "on-request" or result.get("approvalsReviewer") != "user"
                    or not isinstance(result.get("sandbox"), dict) or result["sandbox"].get("type") != "workspaceWrite"):
                raise HostError("NATIVE_EFFECTIVE_CONFIG_MISMATCH")
            self.thread_id = thread["id"]
            _emit(self.output, {"type": "thread.started", "thread_id": self.thread_id})
            self.send("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": cfg["prompt"]}],
                                     "model": cfg["model"], "effort": cfg["effort"],
                                     "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}})
        elif method == "turn/start":
            self._turn(result.get("turn"))
        else:
            raise HostError("NATIVE_PROTOCOL_UNEXPECTED_RESULT")

    def notification(self, method, params):
        cfg = self.configuration
        if method == "thread/settings/updated":
            self._same_thread(params)
            settings = params.get("threadSettings")
            if (not isinstance(settings, dict) or settings.get("model") != cfg["model"]
                    or settings.get("modelProvider") != PROVIDER or settings.get("effort") != cfg["effort"]
                    or settings.get("cwd") != cfg["cwd"] or settings.get("approvalPolicy") != "on-request"
                    or settings.get("approvalsReviewer") != "user"
                    or settings.get("sandboxPolicy") != {"type": "externalSandbox", "networkAccess": "restricted"}):
                raise HostError("NATIVE_EFFECTIVE_CONFIG_MISMATCH")
            self.settings_verified = True
            _emit(self.output, {"type": "host.policy_verified", "thread_id": self.thread_id,
                                "sandbox": "externalSandbox", "network_access": "restricted",
                                "approval_policy": "on-request", "approvals_reviewer": "user"})
        elif method == "turn/started":
            self._same_thread(params)
            self._turn(params.get("turn"))
            if self.started or not self.settings_verified:
                raise HostError("NATIVE_PROTOCOL_INVALID_SEQUENCE")
            self.started = True
            _emit(self.output, {"type": "turn.started", "thread_id": self.thread_id})
        elif method in {"item/started", "item/completed"}:
            self._same_thread(params, turn=True)
            if not self.started:
                raise HostError("NATIVE_PROTOCOL_INVALID_SEQUENCE")
            item = params.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise HostError("NATIVE_PROTOCOL_INVALID_ITEM")
            kind = item["type"]
            if kind == "collabAgentToolCall":
                raise HostError("NATIVE_DELEGATION_FORBIDDEN")
            normalized = {"id": item.get("id"), "type": kind}
            if kind == "agentMessage":
                normalized.update(type="agent_message", text=item.get("text", ""))
            elif kind == "commandExecution":
                normalized.update(type="command_execution", command=item.get("command", ""),
                                  aggregated_output=item.get("aggregatedOutput") or "", exit_code=item.get("exitCode"),
                                  status={"inProgress": "in_progress"}.get(item.get("status"), item.get("status")))
            elif kind == "userMessage":
                return
            _emit(self.output, {"type": "item.started" if method == "item/started" else "item.completed",
                                "thread_id": self.thread_id, "item": normalized})
        elif method == "thread/tokenUsage/updated":
            self._same_thread(params, turn=True)
            usage = params.get("tokenUsage", {}).get("total") if isinstance(params.get("tokenUsage"), dict) else None
            if isinstance(usage, dict):
                self.model_usage = usage
        elif method == "turn/completed":
            self._same_thread(params)
            self._turn(params.get("turn"))
            if (not self.started or not self.settings_verified or self.completed
                    or params["turn"].get("status") != "completed" or params["turn"].get("error") is not None):
                raise HostError("NATIVE_TURN_FAILED")
            self.completed = True
        elif method in {"warning", "error"}:
            # In particular, generic fallback model metadata must not become a
            # successful artifact merely because the provider returned text.
            raise HostError("NATIVE_HOST_WARNING" if method == "warning" else "NATIVE_HOST_ERROR")
        elif method == "thread/started":
            thread = params.get("thread") if isinstance(params, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != self.thread_id:
                raise HostError("NATIVE_IDENTITY_MISMATCH")
        # Other bounded notifications (progress/rate-limits/deltas) carry no
        # authority. They do not create turns or change the verified policy.

    def handle(self, message):
        if "method" in message:
            method = message["method"]
            if (not isinstance(method, str) or not 0 < len(method) <= 256
                    or "result" in message or "error" in message):
                raise HostError("NATIVE_PROTOCOL_INVALID_MESSAGE")
            if "id" in message:
                ident = message["id"]
                if type(ident) not in (int, str) or (isinstance(ident, str) and not 0 < len(ident) <= 256):
                    raise HostError("NATIVE_PROTOCOL_INVALID_MESSAGE")
                self.approvals_denied += 1
                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                    self.write({"id": ident, "result": {"decision": "decline"}})
                else:
                    self.write({"id": ident, "error": {"code": -32601, "message": "Interactive capability is not authorized by this controller."}})
                raise HostError("NATIVE_INTERACTION_REQUIRED")
            self.notification(method, message.get("params"))
            return
        ident = message.get("id")
        if type(ident) is not int or ident not in self.pending or set(message) - {"id", "result", "error"}:
            raise HostError("NATIVE_PROTOCOL_UNEXPECTED_RESULT")
        method = self.pending.pop(ident)
        if "error" in message or "result" not in message:
            raise HostError("NATIVE_PROTOCOL_REQUEST_REJECTED")
        self.result(method, message["result"])

    def _wait_service(self, timeout):
        # Drain pending shutdown notifications instead of deadlocking a child
        # that is waiting to flush its stdout pipe. Do not accumulate tail data.
        deadline = time.monotonic() + timeout
        discarded = 0
        descriptor = self.process.stdout.fileno()
        while self.process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ready, _, _ = select.select([descriptor], [], [], min(remaining, .05))
            if ready:
                chunk = os.read(descriptor, 65536)
                discarded += len(chunk)
                if discarded > MAX_MESSAGE_BYTES:
                    return False
                if not chunk:
                    try:
                        self.process.wait(timeout=max(.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        return False
        return True

    def run(self):
        cfg = self.configuration
        deadline = time.monotonic() + cfg["timeout_seconds"]
        startup_deadline = min(deadline, time.monotonic() + 30)
        self.process = subprocess.Popen([cfg["executable"], "app-server", "--stdio", "--strict-config",
                                         "-c", "agents.enabled=false", *cfg["argv_extra"]],
                                        cwd=cfg["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None, close_fds=True)
        succeeded = False
        service_terminated = False
        service_killed = False
        try:
            self.send("initialize", {"clientInfo": {"name": "xiaolvji_controlled_executor", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
            messages = _Messages(self.process.stdout)
            for _ in range(MAX_MESSAGES):
                self.handle(messages.read(deadline if self.started else startup_deadline))
                if self.completed:
                    if self.pending:
                        raise HostError("NATIVE_PROTOCOL_INVALID_SEQUENCE")
                    succeeded = True
                    break
            if not succeeded:
                raise HostError("NATIVE_PROTOCOL_MESSAGE_LIMIT")
        finally:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            # App-server is a long-lived service; turn completion is its model
            # result. Close our transport, then terminate the owned service if
            # it stays resident. The outside supervisor separately proves the
            # complete process tree exited, including detached descendants.
            if not self._wait_service(1):
                service_terminated = True
                self.process.terminate()
                if not self._wait_service(2):
                    service_killed = True
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process.stdout.close()
        if succeeded:
            expected_signal = signal.SIGKILL if service_killed else signal.SIGTERM if service_terminated else None
            if self.process.returncode != 0 and not (expected_signal and self.process.returncode == -expected_signal):
                raise HostError("NATIVE_SERVICE_STOP_FAILED")
            _emit(self.output, {"type": "host.service_stopped", "thread_id": self.thread_id,
                                "exit_code": self.process.returncode, "native_turn_completed": True,
                                "reason": "controller_shutdown",
                                "signal_used": signal.Signals(expected_signal).name if expected_signal else None,
                                "process_tree_terminated": False})
            _emit(self.output, {"type": "turn.completed", "thread_id": self.thread_id,
                                "usage": self.model_usage or {}})
        return 0


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise HostError("NATIVE_INPUT_INVALID")
        Client(_json(raw), sys.stdout).run()
        return 0
    except (HostError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, HostError) else "NATIVE_LAUNCH_FAILED"
        _emit(sys.stdout, {"type": "error", "code": code, "message": "Native Codex execution did not pass the controlled protocol checks."})
        _emit(sys.stdout, {"type": "turn.failed", "error": {"code": code, "message": "Native host execution failed."}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
