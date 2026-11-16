"""Controller-owned, bounded model transport over one inherited socketpair per attempt.

The worker receives a socket endpoint and a revocable attempt capability, never an
upstream credential.  This module does not read environment variables, auth files,
or host client configuration.  Its process, journal, credentials and admission
callback belong to the trusted controller outside the worker sandbox.

Protocol v1 uses a four-byte unsigned big-endian length followed by strict UTF-8
JSON.  Each request has v, request_id, capability, method, path and body.  Replies
are response, data (base64), and terminal end or error frames.  A channel permits
one request at a time; callers must wait for the terminal frame before sending
another request.  Revocation closes the channel and local upstream connections.
It cannot establish that an upstream provider stopped work after disconnection.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import stat
import struct
import time
from typing import Any, Callable, Mapping
from types import MappingProxyType
from urllib.parse import urlsplit

from .contracts import Fault, canonical_json, digest, load_json, validate_identifier

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024 + 65536
MAX_HEADER_BYTES = 32768
MAX_HEADER_LINE = 8192
MAX_HEADER_COUNT = 64
DATA_CHUNK_BYTES = 16384
API_ROUTES = {
    "openai-responses": "/v1/responses",
    "openai-chat-completions": "/v1/chat/completions",
    "anthropic-messages": "/v1/messages",
}
_REQUEST_FIELDS = {
    "openai-responses": {
        "model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls",
        "reasoning", "text", "stream", "store", "include", "max_output_tokens",
        "temperature", "top_p", "service_tier", "metadata", "user", "prompt_cache_key",
        "prompt_cache_retention", "safety_identifier", "truncation", "stream_options", "client_metadata",
    },
    "openai-chat-completions": {
        "model", "messages", "tools", "tool_choice", "parallel_tool_calls", "reasoning_effort",
        "stream", "stream_options", "max_tokens", "max_completion_tokens", "temperature",
        "top_p", "n", "stop", "presence_penalty", "frequency_penalty", "logit_bias",
        "logprobs", "top_logprobs", "response_format", "seed", "user", "metadata",
        "service_tier", "store", "verbosity", "prediction",
    },
    "anthropic-messages": {
        "model", "messages", "system", "max_tokens", "metadata", "stop_sequences",
        "stream", "temperature", "top_k", "top_p", "tools", "tool_choice", "thinking",
        "output_config", "service_tier",
    },
}
_CONSTRAINT_FIELDS = {
    "openai-responses": {"reasoning", "text", "max_output_tokens", "temperature", "top_p", "service_tier"},
    "openai-chat-completions": {"reasoning_effort", "max_tokens", "max_completion_tokens", "temperature", "top_p", "service_tier", "verbosity"},
    "anthropic-messages": {"thinking", "output_config", "max_tokens", "temperature", "top_k", "top_p", "service_tier"},
}
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _invalid(message: str) -> Fault:
    return Fault("INVALID_PROVIDER_PROFILE", message)


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid(f"{label} is outside its supported integer range.")
    return value


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    api: str
    upstream_url: str
    model: str
    request_constraints: dict[str, Any]
    max_requests: int = 32
    max_request_bytes: int = 2 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    request_timeout: float = 120
    max_concurrency: int = 4
    _constraints_json: str = field(init=False, repr=False)

    def __post_init__(self):
        try:
            validate_identifier(self.profile_id, "provider profile identifier")
        except Fault:
            raise _invalid("Provider profile identifier is invalid.") from None
        if not isinstance(self.api, str) or self.api not in API_ROUTES:
            raise _invalid("Provider API is not supported.")
        if not isinstance(self.upstream_url, str) or len(self.upstream_url) > 2048:
            raise _invalid("Provider URL must be an explicit HTTPS endpoint.")
        try:
            url = urlsplit(self.upstream_url)
            host = url.hostname
            port = url.port
            if (url.scheme != "https" or not host or url.username is not None or url.password is not None
                    or url.query or url.fragment or not url.path.startswith("/")
                    or "\\" in self.upstream_url or any(ord(c) <= 32 or ord(c) > 126 for c in self.upstream_url)
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError()
            host.encode("ascii")
        except (ValueError, UnicodeError):
            raise _invalid("Provider URL must be a credential-free HTTPS endpoint without query or fragment.") from None
        if not isinstance(self.model, str) or not self.model or len(self.model) > 256 or any(ord(c) < 32 for c in self.model):
            raise _invalid("An explicit model identifier is required.")
        try:
            self.model.encode("utf-8")
        except UnicodeError:
            raise _invalid("Model identifier must contain valid Unicode scalar values.") from None
        if not isinstance(self.request_constraints, dict) or set(self.request_constraints) - _CONSTRAINT_FIELDS[self.api]:
            raise _invalid("Request constraints contain unsupported fields.")
        try:
            encoded = canonical_json(self.request_constraints)
            load_json(encoded, limit=16384)
        except Fault:
            raise _invalid("Request constraints must be bounded finite JSON.") from None
        if any(value is None for value in self.request_constraints.values()):
            raise _invalid("Pinned request constraints cannot be null.")
        object.__setattr__(self, "_constraints_json", encoded)
        # Retain a detached convenience view; all policy decisions use the frozen
        # canonical snapshot so mutating a caller's dictionary changes no grant.
        object.__setattr__(self, "request_constraints", load_json(encoded))
        _integer(self.max_requests, 1, 1024, "max_requests")
        _integer(self.max_request_bytes, 64, 16 * 1024 * 1024, "max_request_bytes")
        _integer(self.max_response_bytes, 1, 64 * 1024 * 1024, "max_response_bytes")
        _integer(self.max_concurrency, 1, 64, "max_concurrency")
        if (type(self.request_timeout) not in (int, float) or not math.isfinite(self.request_timeout)
                or not 0 < self.request_timeout <= 3600):
            raise _invalid("request_timeout must be positive, finite and at most 3600 seconds.")

    def to_dict(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, "api": self.api, "upstream_url": self.upstream_url,
                "model": self.model, "request_constraints": load_json(self._constraints_json),
                "max_requests": self.max_requests, "max_request_bytes": self.max_request_bytes,
                "max_response_bytes": self.max_response_bytes, "request_timeout": self.request_timeout,
                "max_concurrency": self.max_concurrency}


def normalize_provider_profile(value: dict[str, Any]) -> ProviderProfile:
    allowed = {"profile_id", "api", "upstream_url", "model", "request_constraints", "max_requests",
               "max_request_bytes", "max_response_bytes", "request_timeout", "max_concurrency"}
    required = {"profile_id", "api", "upstream_url", "model", "request_constraints"}
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise _invalid("Provider profile fields do not match the closed schema.")
    try:
        return ProviderProfile(**value)
    except (TypeError, ValueError, UnicodeError):
        raise _invalid("Provider profile contains invalid values.") from None


def _binding(attempt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(attempt, dict):
        raise Fault("INVALID_BROKER_BINDING", "An admitted attempt binding is required.")
    try:
        validate_identifier(attempt.get("attempt_id"), "attempt identifier")
    except Fault:
        raise Fault("INVALID_BROKER_BINDING", "Invalid attempt identifier.") from None
    generation, launch_key = attempt.get("generation"), attempt.get("launch_key")
    if (type(generation) is not int or not 1 <= generation <= 2**31 - 1
            or not isinstance(launch_key, str) or not launch_key or len(launch_key) > 256
            or any(ord(c) < 32 for c in launch_key)):
        raise Fault("INVALID_BROKER_BINDING", "Invalid attempt generation or launch key.")
    binding = {"attempt_id": attempt["attempt_id"], "generation": generation, "launch_key": launch_key}
    try:
        canonical_json(binding)
    except Fault:
        raise Fault("INVALID_BROKER_BINDING", "Attempt binding must contain valid Unicode scalar values.") from None
    return binding


@dataclass
class Grant:
    binding: dict[str, Any]
    capability: str = field(repr=False)
    policy_digest: str
    worker_socket: socket.socket = field(repr=False)

    def close_worker_endpoint(self) -> None:
        """Call after handing a duplicated descriptor to the trusted supervisor."""
        self.worker_socket.close()


@dataclass
class _Lease:
    grant_id: str
    binding: dict[str, Any]
    profile: ProviderProfile
    capability_hash: str
    deadline: float
    task: asyncio.Task | None = None
    request_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    writer: asyncio.StreamWriter | None = None
    upstream: asyncio.StreamWriter | None = None
    revoked: bool = False
    response_bytes: int = 0


async def read_frame(reader: asyncio.StreamReader, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size == 0 or size > max_bytes:
        raise Fault("BROKER_PROTOCOL_ERROR", "Protocol frame exceeds its permitted size.")
    raw = await reader.readexactly(size)
    try:
        value = load_json(raw, limit=max_bytes)
    except Fault:
        raise Fault("BROKER_PROTOCOL_ERROR", "Protocol frame is not strict JSON.") from None
    if not isinstance(value, dict):
        raise Fault("BROKER_PROTOCOL_ERROR", "Protocol frame must be an object.")
    return value


async def write_frame(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    payload = canonical_json(frame).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise Fault("BROKER_PROTOCOL_ERROR", "Protocol output exceeds its permitted size.")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


def _subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(actual[key], value) for key, value in expected.items())
    return type(actual) is type(expected) and actual == expected


def _validate_body(profile: ProviderProfile, body: Any) -> bytes:
    if not isinstance(body, dict) or set(body) - _REQUEST_FIELDS[profile.api]:
        raise Fault("BROKER_REQUEST_REJECTED", "Request contains unsupported fields.")
    if body.get("model") != profile.model or type(body.get("model")) is not str:
        raise Fault("BROKER_POLICY_MISMATCH", "Request model differs from the pinned model.")
    if not _subset(body, load_json(profile._constraints_json)):
        raise Fault("BROKER_POLICY_MISMATCH", "Request differs from the pinned quality settings.")
    if "stream" in body and type(body["stream"]) is not bool:
        raise Fault("BROKER_REQUEST_REJECTED", "stream must be a boolean.")
    if "client_metadata" in body:
        metadata = body["client_metadata"]
        # Native Codex includes request/thread telemetry as a string map. It is
        # opaque data: never interpret it as routing, tools, or policy. Bound
        # it independently of the body limit and reject nested objects/types.
        if (not isinstance(metadata, dict) or len(metadata) > 32
                or any(not isinstance(key, str) or not 1 <= len(key.encode("utf-8")) <= 128
                       or not isinstance(value, str) or len(value.encode("utf-8")) > 4096
                       for key, value in metadata.items())
                or len(canonical_json(metadata).encode("utf-8")) > 32768):
            raise Fault("BROKER_REQUEST_REJECTED", "Native client metadata exceeds its bounded string-map shape.")
    if ("store" in body and body["store"] is not False) or ("n" in body and (type(body["n"]) is not int or body["n"] != 1)):
        raise Fault("BROKER_REQUEST_REJECTED", "Stored or multiple generated responses are not supported.")
    content = body.get("input" if profile.api == "openai-responses" else "messages")
    if not isinstance(content, (str, list)) or (profile.api != "openai-responses" and not isinstance(content, list)):
        raise Fault("BROKER_REQUEST_REJECTED", "Request input has an unsupported shape.")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 256:
        raise Fault("BROKER_REQUEST_REJECTED", "Request tools have an unsupported shape.")
    for tool in tools:
        if not isinstance(tool, dict):
            raise Fault("BROKER_REQUEST_REJECTED", "Request tools have an unsupported shape.")
        allowed = {"function", "custom"} if profile.api == "openai-responses" else {"function"}
        if profile.api == "anthropic-messages":
            allowed = {None, "custom"}
        kind = tool.get("type")
        if (kind is not None and not isinstance(kind, str)) or kind not in allowed:
            raise Fault("BROKER_REQUEST_REJECTED", "Provider-managed remote tools are not permitted.")
    # Providers execute URL fetching and file references themselves.  This
    # transport supports inline inputs and client-executed tools only.
    pending = [content]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(key in item for key in ("file_id", "file_url", "image_url", "audio_url", "video_url")):
                for key in ("file_id", "file_url", "image_url", "audio_url", "video_url"):
                    if key in item and not (key == "image_url" and isinstance(item[key], str) and item[key].startswith("data:")):
                        raise Fault("BROKER_REQUEST_REJECTED", "Remote input references are not permitted.")
            if item.get("type") == "url" or item.get("type") == "item_reference":
                raise Fault("BROKER_REQUEST_REJECTED", "Remote input references are not permitted.")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    encoded = canonical_json(body).encode("utf-8")
    if len(encoded) > profile.max_request_bytes:
        raise Fault("BROKER_REQUEST_TOO_LARGE", "Request body exceeds the configured byte budget.")
    return encoded


class _Redactor:
    """Remove exact raw/JSON-escaped credentials across arbitrary stream chunks."""
    def __init__(self, secrets_to_hide: list[str]):
        needles = set()
        for value in secrets_to_hide:
            needles.add(value.encode("utf-8"))
            needles.add(canonical_json(value)[1:-1].encode("utf-8"))
        self.needles = sorted(needles, key=len, reverse=True)
        self.keep = max(map(len, self.needles), default=1) - 1
        self.pending = b""

    def feed(self, data: bytes, final: bool = False) -> bytes:
        data = self.pending + data
        boundary = len(data) if final else max(0, len(data) - self.keep)
        output = bytearray()
        offset = 0
        while offset < boundary:
            found = next((needle for needle in self.needles if data.startswith(needle, offset)), None)
            if found is None:
                output.append(data[offset])
                offset += 1
            else:
                output.extend(b"[REDACTED]")
                offset += len(found)
        self.pending = data[offset:]
        return bytes(output)


class ProviderBroker:
    def __init__(self, state_dir: str | Path,
                 profiles: Mapping[str, ProviderProfile | dict[str, Any]], credentials: Mapping[str, str],
                 admission_check: Callable[[dict[str, Any], str], Any], global_concurrency: int = 8):
        self.root = Path(state_dir).absolute()
        if self.root.resolve() != self.root:
            raise Fault("UNSAFE_STATE", "Broker state must not contain symlink aliases.")
        if not isinstance(profiles, Mapping) or not profiles or not isinstance(credentials, Mapping) or set(credentials) != set(profiles):
            raise _invalid("Every registered provider requires exactly one explicit credential.")
        self.profiles = {}
        self._credentials = {}
        for name, profile in profiles.items():
            value = normalize_provider_profile(profile.to_dict() if isinstance(profile, ProviderProfile) else profile)
            if name != value.profile_id:
                raise _invalid("Provider registration key does not match its profile identifier.")
            credential = credentials[name]
            if (not isinstance(credential, str) or not 8 <= len(credential) <= 8192
                    or any(ord(c) < 33 or ord(c) > 126 for c in credential)):
                raise _invalid("Provider credential must be a bounded nonempty ASCII token.")
            self.profiles[name] = value
            self._credentials[name] = credential
        self.profiles = MappingProxyType(self.profiles)
        if not callable(admission_check):
            raise _invalid("A trusted admission callback is required.")
        self._admission = admission_check
        self.global_concurrency = _integer(global_concurrency, 1, 64, "global_concurrency")
        self._db: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        self._epoch = secrets.token_hex(16)
        self._leases: dict[tuple[str, int], _Lease] = {}
        self._active = 0
        self._provider_active = {name: 0 for name in self.profiles}
        self._watchdog: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._closing = False
        self._journal_failed = False

    @staticmethod
    def _private_file(path: Path) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(fd)
            raise Fault("UNSAFE_STATE", "Broker journal files require private ownership and a single link.")
        return fd

    async def start(self) -> None:
        if self._db is not None or self._closing:
            raise Fault("BROKER_STATE_ERROR", "Broker cannot be started in its current state.")
        try:
            import fcntl
        except ImportError:
            raise Fault("CAPABILITY_UNSUPPORTED", "Provider execution requires POSIX journal locking and inherited Unix IPC.") from None
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise Fault("UNSAFE_STATE", "Broker state directory must be private to the controller user.")
            self._lock_fd = self._private_file(self.root / "owner.lock")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Fault("BROKER_ALREADY_OWNED", "Another controller owns the provider broker journal.") from None
            for name in ("journal.sqlite3", "journal.sqlite3-wal", "journal.sqlite3-shm"):
                path = self.root / name
                if name == "journal.sqlite3" or path.exists() or path.is_symlink():
                    os.close(self._private_file(path))
            self._db = sqlite3.connect(self.root / "journal.sqlite3", isolation_level=None, timeout=1)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS grants(
                    grant_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    launch_key TEXT NOT NULL, epoch TEXT NOT NULL, profile_id TEXT NOT NULL,
                    policy_digest TEXT NOT NULL, capability_hash TEXT NOT NULL, status TEXT NOT NULL,
                    issued_at REAL NOT NULL, expires_at REAL NOT NULL, requests_consumed INTEGER NOT NULL DEFAULT 0,
                    reason TEXT, UNIQUE(attempt_id,generation));
                CREATE TABLE IF NOT EXISTS requests(
                    grant_id TEXT NOT NULL, request_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    state TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                    response_bytes INTEGER NOT NULL DEFAULT 0, outcome TEXT,
                    PRIMARY KEY(grant_id,request_id));
            """)
            self._transaction(lambda db: (
                db.execute("UPDATE grants SET status='REVOKED',reason='owner_restarted' WHERE status='ACTIVE'"),
                db.execute("UPDATE requests SET state='UNCERTAIN',finished_at=?,outcome='owner_restarted' WHERE state='IN_FLIGHT'", (time.time(),)),
            ))
            self._watchdog = asyncio.create_task(self._watch(), name="xiaolvji-provider-watchdog")
        except BaseException:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            raise

    def _transaction(self, operation):
        if self._db is None or self._journal_failed:
            raise Fault("BROKER_STATE_ERROR", "Provider journal is unavailable.")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            result = operation(self._db)
            self._db.execute("COMMIT")
            return result
        except Fault:
            self._db.execute("ROLLBACK")
            raise
        except sqlite3.Error:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._journal_failed = True
            # Lost accounting authority denies all further traffic, including
            # streams already admitted. Never retry a possibly committed call.
            for lease in self._leases.values():
                self._stop_lease(lease)
            raise Fault("BROKER_JOURNAL_UNCERTAIN", "Provider accounting could not be durably recorded.") from None

    async def _admitted(self, binding: dict[str, Any], phase: str) -> bool:
        try:
            result = self._admission(dict(binding), phase)
            if inspect.isawaitable(result):
                async with asyncio.timeout(1):
                    result = await result
            return result is True
        except Exception:
            return False

    async def grant(self, attempt: dict[str, Any], provider_id: str, *, ttl_seconds: float = 3600) -> Grant:
        if self._db is None or self._closing or self._journal_failed:
            raise Fault("BROKER_STATE_ERROR", "Provider broker is not accepting grants.")
        binding = _binding(attempt)
        if not isinstance(provider_id, str) or provider_id not in self.profiles:
            raise Fault("PROVIDER_NOT_REGISTERED", "Provider profile has not been explicitly registered.")
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 86400:
            raise Fault("INVALID_BROKER_BINDING", "Grant lifetime must be positive, finite and at most one day.")
        if not await self._admitted(binding, "grant"):
            raise Fault("BROKER_ADMISSION_DENIED", "Attempt is not currently admitted for provider transport.")
        if self._closing or self._journal_failed or len(self._leases) >= 256:
            raise Fault("BROKER_STATE_ERROR", "Provider broker is not accepting grants.")
        profile = self.profiles[provider_id]
        capability = secrets.token_urlsafe(32)
        cap_hash = hashlib.sha256(capability.encode("ascii")).hexdigest()
        grant_id = secrets.token_hex(16)
        now = time.time()
        policy_digest = digest({"version": 1, "binding": binding, "profile": profile.to_dict(),
                                "epoch": self._epoch, "grant_id": grant_id, "expires_at": now + ttl_seconds})
        try:
            parent, worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            parent.setblocking(False)
            parent.set_inheritable(False)
            worker.set_inheritable(False)
        except OSError:
            raise Fault("CAPABILITY_UNSUPPORTED", "Inherited Unix socketpair transport is unavailable.") from None
        try:
            def record(db):
                if db.execute("SELECT 1 FROM grants WHERE attempt_id=? AND generation=?", (binding["attempt_id"], binding["generation"])).fetchone():
                    raise Fault("BROKER_GRANT_REUSED", "An attempt generation cannot receive another provider grant.")
                db.execute("INSERT INTO grants(grant_id,attempt_id,generation,launch_key,epoch,profile_id,policy_digest,capability_hash,status,issued_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (grant_id, binding["attempt_id"], binding["generation"], binding["launch_key"], self._epoch,
                            profile.profile_id, policy_digest, cap_hash, "ACTIVE", now, now + ttl_seconds))
            self._transaction(record)
            reader, writer = await asyncio.open_connection(sock=parent, limit=MAX_HEADER_BYTES + 1)
            if self._closing or self._journal_failed or self._db is None:
                writer.transport.abort()
                raise Fault("BROKER_STATE_ERROR", "Provider broker stopped during grant setup.")
            lease = _Lease(grant_id, dict(binding), profile, cap_hash, time.monotonic() + ttl_seconds, writer=writer)
            key = (binding["attempt_id"], binding["generation"])
            self._leases[key] = lease
            lease.task = asyncio.create_task(self._handle(lease, reader, writer), name="xiaolvji-provider-" + binding["attempt_id"])
            return Grant(dict(binding), capability, policy_digest, worker)
        except BaseException:
            parent.close()
            worker.close()
            if self._db is not None and not self._journal_failed:
                self._transaction(lambda db: db.execute("UPDATE grants SET status='REVOKED',reason='grant_setup_failed' WHERE grant_id=?", (grant_id,)))
            raise

    @staticmethod
    def _stop_lease(lease: _Lease) -> None:
        if lease.revoked:
            # A second Task.cancel() can interrupt the first cancellation's
            # finally block while it joins the upstream task. Never interrupt
            # our own accounting/transport cleanup with repeated revocations.
            return
        lease.revoked = True
        if lease.upstream is not None:
            # abort() discards pending writes and does not wait for a remote TLS
            # close-notify handshake. A revoked channel must lose local I/O
            # authority promptly, including during blocked send/drain calls.
            lease.upstream.transport.abort()
        if lease.writer is not None:
            lease.writer.transport.abort()
        current = asyncio.current_task()
        if lease.request_task is not None and lease.request_task is not current:
            lease.request_task.cancel()
        if lease.task is not None and lease.task is not current:
            lease.task.cancel()

    async def revoke(self, attempt_id: str, generation: int, reason: str = "revoked") -> None:
        # Caller-provided details are not journaled: they can contain prompts or
        # credentials. Only this fixed reason vocabulary crosses the boundary.
        allowed = {"revoked", "expired", "admission_lost", "controller_closed", "worker_disconnected", "protocol_error"}
        reason = reason if reason in allowed else "revoked"
        lease = self._leases.get((attempt_id, generation))
        if lease is None:
            return
        self._stop_lease(lease)
        if self._db is not None and not self._journal_failed:
            self._transaction(lambda db: db.execute("UPDATE grants SET status='REVOKED',reason=? WHERE grant_id=? AND status='ACTIVE'", (reason, lease.grant_id)))
        if lease.cleanup_task is None:
            lease.cleanup_task = asyncio.create_task(self._finish_revoke(lease), name="xiaolvji-provider-revoke")
        # Cancelling the caller must not propagate another cancellation into
        # cleanup or prevent retirement of a never-started handler's lease.
        await asyncio.shield(lease.cleanup_task)

    async def _finish_revoke(self, lease: _Lease) -> None:
        pending = [task for task in (lease.request_task, lease.task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Task.cancel() before a coroutine's first turn never enters its finally
        # block. Retire the proven-finished lease independently of _handle;
        # durable grant/request history still prevents generation/token replay.
        key = (lease.binding["attempt_id"], lease.binding["generation"])
        if self._leases.get(key) is lease:
            self._leases.pop(key)

    async def _watch(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(0.05)
                for lease in list(self._leases.values()):
                    if lease.revoked:
                        continue
                    if time.monotonic() >= lease.deadline:
                        await self.revoke(lease.binding["attempt_id"], lease.binding["generation"], "expired")
                    elif not await self._admitted(lease.binding, "request"):
                        # The short pre-launch interval is still eligible for a
                        # grant, but no request can be made until RUNNING.
                        if not await self._admitted(lease.binding, "grant"):
                            await self.revoke(lease.binding["attempt_id"], lease.binding["generation"], "admission_lost")
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            # Deauthorize every channel before the first await. The separately
            # owned close task finishes even if its caller is cancelled.
            for lease in list(self._leases.values()):
                self._stop_lease(lease)
            self._close_task = asyncio.create_task(self._finish_close(), name="xiaolvji-provider-close")
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        leases = list(self._leases.values())
        try:
            if self._watchdog is not None:
                self._watchdog.cancel()
                await asyncio.gather(self._watchdog, return_exceptions=True)
            for lease in leases:
                await self.revoke(lease.binding["attempt_id"], lease.binding["generation"], "controller_closed")
        finally:
            # Journal failure must not prevent descriptor/credential cleanup.
            # Every task was cancelled exactly once before close's first await.
            pending = {task for lease in leases for task in (lease.request_task, lease.task) if task is not None}
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                if self._db is not None:
                    self._db.close()
            finally:
                self._db = None
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                self._credentials.clear()

    def status(self) -> dict[str, Any]:
        if self._db is None:
            raise Fault("BROKER_STATE_ERROR", "Provider broker has not been started.")
        return {"version": 1, "journal_healthy": not self._journal_failed, "global_in_flight": self._active,
                "provider_in_flight": dict(self._provider_active),
                "grants": [dict(row) for row in self._db.execute(
                    "SELECT attempt_id,generation,profile_id,policy_digest,status,requests_consumed,reason FROM grants ORDER BY issued_at")],
                "requests": [dict(row) for row in self._db.execute(
                    "SELECT request_id,ordinal,state,response_bytes,outcome FROM requests ORDER BY started_at")],
                "remote_processing_after_disconnect": "unknown"}

    async def _error(self, writer: asyncio.StreamWriter, request_id: str, fault: Fault) -> None:
        async with asyncio.timeout(0.5):
            await write_frame(writer, {"v": 1, "type": "error", "request_id": request_id,
                                       "code": fault.code, "message": fault.message})

    async def _handle(self, lease: _Lease, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        reason = "worker_disconnected"
        try:
            while not lease.revoked and not self._closing:
                request_id = ""
                try:
                    async with asyncio.timeout(max(0.001, lease.deadline - time.monotonic())):
                        frame = await read_frame(reader, lease.profile.max_request_bytes + 4096)
                    if isinstance(frame.get("request_id"), str) and _REQUEST_ID.fullmatch(frame["request_id"]):
                        request_id = frame["request_id"]
                    if (set(frame) != {"v", "request_id", "capability", "method", "path", "body"}
                            or type(frame["v"]) is not int or frame["v"] != 1 or not request_id):
                        raise Fault("BROKER_PROTOCOL_ERROR", "Request does not match protocol version 1.")
                    token = frame["capability"]
                    if (not isinstance(token, str) or not _CAPABILITY.fullmatch(token)
                            or not hmac.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), lease.capability_hash)):
                        raise Fault("BROKER_UNAUTHORIZED", "Attempt capability is invalid.")
                    if frame["method"] != "POST" or frame["path"] != API_ROUTES[lease.profile.api]:
                        raise Fault("BROKER_REQUEST_REJECTED", "Only the registered model route is permitted.")
                    body = _validate_body(lease.profile, frame["body"])
                    if lease.revoked or time.monotonic() >= lease.deadline or not await self._admitted(lease.binding, "request"):
                        raise Fault("BROKER_ADMISSION_DENIED", "Attempt is not currently admitted for model requests.")
                    if self._active >= self.global_concurrency or self._provider_active[lease.profile.profile_id] >= lease.profile.max_concurrency:
                        raise Fault("BROKER_BUSY", "Local model request capacity is occupied.")
                    def consume(db):
                        row = db.execute("SELECT requests_consumed,status FROM grants WHERE grant_id=?", (lease.grant_id,)).fetchone()
                        if row is None or row["status"] != "ACTIVE":
                            raise Fault("BROKER_UNAUTHORIZED", "Attempt capability has been revoked.")
                        if db.execute("SELECT 1 FROM requests WHERE grant_id=? AND request_id=?", (lease.grant_id, request_id)).fetchone():
                            raise Fault("BROKER_REQUEST_REPLAY", "Request identifier has already been consumed.")
                        if row["requests_consumed"] >= lease.profile.max_requests:
                            raise Fault("BROKER_BUDGET_EXHAUSTED", "Attempt model request budget is exhausted.")
                        ordinal = row["requests_consumed"] + 1
                        db.execute("UPDATE grants SET requests_consumed=? WHERE grant_id=?", (ordinal, lease.grant_id))
                        db.execute("INSERT INTO requests(grant_id,request_id,ordinal,state,started_at) VALUES(?,?,?,'IN_FLIGHT',?)", (lease.grant_id, request_id, ordinal, time.time()))
                    self._transaction(consume)
                    await self._request(lease, reader, writer, request_id, body)
                except Fault as exc:
                    await self._error(writer, request_id, exc)
                    if exc.code in {"BROKER_PROTOCOL_ERROR", "BROKER_UNAUTHORIZED", "BROKER_JOURNAL_UNCERTAIN"}:
                        reason = "protocol_error"
                        break
                except TimeoutError:
                    reason = "expired"
                    break
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            pass
        finally:
            self._stop_lease(lease)
            if self._db is not None and not self._journal_failed:
                try:
                    self._transaction(lambda db: db.execute("UPDATE grants SET status='REVOKED',reason=? WHERE grant_id=? AND status='ACTIVE'", (reason, lease.grant_id)))
                except Fault:
                    pass
            self._leases.pop((lease.binding["attempt_id"], lease.binding["generation"]), None)
            writer.close()
            try:
                async with asyncio.timeout(0.5):
                    await writer.wait_closed()
            except (Exception, asyncio.CancelledError):
                pass

    async def _request(self, lease: _Lease, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       request_id: str, body: bytes) -> None:
        self._active += 1
        self._provider_active[lease.profile.profile_id] += 1
        outcome, count = "local_cancelled", 0
        lease.response_bytes = 0
        monitor = asyncio.create_task(reader.read(1))
        lease.request_task = asyncio.create_task(self._forward(lease, writer, request_id, body))
        fault = None
        try:
            deadline = min(lease.profile.request_timeout, max(0.001, lease.deadline - time.monotonic()))
            async with asyncio.timeout(deadline):
                done, _ = await asyncio.wait({monitor, lease.request_task}, return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    extra = monitor.result()
                    if extra:
                        fault = Fault("BROKER_PROTOCOL_ERROR", "Pipelined requests are not permitted.")
                    else:
                        raise ConnectionError("worker disconnected")
                else:
                    count = await lease.request_task
                    outcome = "completed"
        except TimeoutError:
            fault = Fault("BROKER_REQUEST_TIMEOUT", "Model request exceeded its configured lifetime.")
        except Fault as exc:
            fault = exc
        except (ConnectionError, OSError, ssl.SSLError, ValueError):
            fault = Fault("BROKER_UPSTREAM_ERROR", "Model transport failed; the request budget remains consumed.")
        finally:
            monitor.cancel()
            lease.request_task.cancel()
            await asyncio.gather(monitor, lease.request_task, return_exceptions=True)
            lease.request_task = None
            if lease.upstream is not None:
                lease.upstream.transport.abort()
                lease.upstream = None
            count = lease.response_bytes
            self._active -= 1
            self._provider_active[lease.profile.profile_id] -= 1
            if fault is not None:
                outcome = fault.code
            if self._db is not None and not self._journal_failed:
                self._transaction(lambda db: db.execute(
                    "UPDATE requests SET state=?,finished_at=?,response_bytes=?,outcome=? WHERE grant_id=? AND request_id=?",
                    ("COMPLETED" if outcome == "completed" else "LOCAL_ENDED", time.time(), count, outcome, lease.grant_id, request_id)))
        if fault is not None:
            raise fault
        async with asyncio.timeout(0.5):
            await write_frame(writer, {"v": 1, "type": "end", "request_id": request_id})

    async def _connect_upstream(self, profile: ProviderProfile):
        """Private seam for synthetic loopback fixtures; production always uses verified TLS."""
        url = urlsplit(profile.upstream_url)
        return await asyncio.open_connection(url.hostname, url.port or 443, ssl=ssl.create_default_context(),
                                             server_hostname=url.hostname, limit=MAX_HEADER_BYTES + 1)

    async def _forward(self, lease: _Lease, downstream: asyncio.StreamWriter, request_id: str, body: bytes) -> int:
        profile = lease.profile
        credential = self._credentials[profile.profile_id]
        reader, upstream = await self._connect_upstream(profile)
        lease.upstream = upstream
        if lease.revoked:
            raise Fault("BROKER_UNAUTHORIZED", "Attempt capability has been revoked.")
        url = urlsplit(profile.upstream_url)
        auth = (f"x-api-key: {credential}\r\nanthropic-version: 2023-06-01\r\n" if profile.api == "anthropic-messages"
                else f"Authorization: Bearer {credential}\r\n")
        headers = (f"POST {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\n"
                   "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
                   f"Accept-Encoding: identity\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{auth}\r\n")
        upstream.write(headers.encode("ascii") + body)
        await upstream.drain()
        status, response_headers = await _read_headers(reader)
        if not 200 <= status < 300:
            raise Fault("BROKER_UPSTREAM_REJECTED", "Provider rejected the model request; diagnostic response bodies are withheld.")
        content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/json", "text/event-stream"}:
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider returned an unsupported content type.")
        if response_headers.get("content-encoding", "identity").lower() != "identity":
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Compressed provider responses are not supported.")
        length = response_headers.get("content-length")
        transfer = response_headers.get("transfer-encoding")
        if transfer is not None and (transfer.lower() != "chunked" or length is not None):
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response framing is ambiguous or unsupported.")
        if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > profile.max_response_bytes):
            raise Fault("BROKER_RESPONSE_TOO_LARGE", "Provider response exceeds its byte budget.")
        await write_frame(downstream, {"v": 1, "type": "response", "request_id": request_id,
                                       "status": status, "headers": {"content-type": content_type}})
        redactor = _Redactor(list(self._credentials.values()))
        total = 0
        async for chunk in _response_chunks(reader, response_headers, profile.max_response_bytes):
            total += len(chunk)
            lease.response_bytes = total
            sanitized = redactor.feed(chunk)
            if sanitized:
                await write_frame(downstream, {"v": 1, "type": "data", "request_id": request_id,
                                               "data": base64.b64encode(sanitized).decode("ascii")})
        tail = redactor.feed(b"", final=True)
        if tail:
            await write_frame(downstream, {"v": 1, "type": "data", "request_id": request_id,
                                           "data": base64.b64encode(tail).decode("ascii")})
        return total


async def _http_line(reader: asyncio.StreamReader) -> bytes:
    try:
        line = await reader.readuntil(b"\r\n")
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
        raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response has an incomplete or oversized HTTP line.") from None
    if len(line) > MAX_HEADER_LINE or b"\r" in line[:-2] or b"\n" in line[:-2]:
        raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response contains an invalid HTTP line.")
    return line


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    first = await _http_line(reader)
    if not re.fullmatch(rb"HTTP/1\.[01] [0-9]{3}(?: [\x20-\x7e]*)?\r\n", first):
        raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider returned an unsupported HTTP status line.")
    status = int(first[9:12])
    headers = {}
    size = len(first)
    count = 0
    while True:
        line = await _http_line(reader)
        size += len(line)
        if size > MAX_HEADER_BYTES or count > MAX_HEADER_COUNT:
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response headers exceed their limit.")
        if line == b"\r\n":
            break
        count += 1
        key, sep, value = line[:-2].partition(b":")
        if not sep or not _HEADER_NAME.fullmatch(key) or any(c < 32 or c > 126 for c in value):
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response headers are malformed.")
        name = key.decode("ascii").lower()
        if name in headers and name in {"content-length", "transfer-encoding", "content-type", "content-encoding"}:
            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider returned duplicate framing headers.")
        headers[name] = value.decode("ascii").strip()
    return status, headers


async def _response_chunks(reader: asyncio.StreamReader, headers: dict[str, str], limit: int):
    total = 0
    try:
        if "content-length" in headers:
            remaining = int(headers["content-length"])
            while remaining:
                chunk = await reader.readexactly(min(remaining, DATA_CHUNK_BYTES))
                remaining -= len(chunk)
                yield chunk
        elif "transfer-encoding" in headers:
            while True:
                line = await _http_line(reader)
                # Chunk extensions are unnecessary for model SSE and deliberately
                # unsupported. This avoids a second unbounded metadata grammar.
                if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line):
                    raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider chunk framing is unsupported.")
                remaining = int(line[:-2], 16)
                if remaining == 0:
                    trailer_size, trailer_count = 0, 0
                    while True:
                        trailer = await _http_line(reader)
                        trailer_size += len(trailer)
                        trailer_count += 1
                        if trailer_size > MAX_HEADER_BYTES or trailer_count > MAX_HEADER_COUNT:
                            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider trailers exceed their limit.")
                        if trailer == b"\r\n":
                            return
                        key, sep, value = trailer[:-2].partition(b":")
                        if not sep or not _HEADER_NAME.fullmatch(key) or any(c < 32 or c > 126 for c in value):
                            raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider trailers are malformed.")
                total += remaining
                if total > limit:
                    raise Fault("BROKER_RESPONSE_TOO_LARGE", "Provider stream exceeds its byte budget.")
                while remaining:
                    chunk = await reader.readexactly(min(remaining, DATA_CHUNK_BYTES))
                    remaining -= len(chunk)
                    yield chunk
                if await reader.readexactly(2) != b"\r\n":
                    raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider chunk terminator is malformed.")
        else:
            while True:
                chunk = await reader.read(min(DATA_CHUNK_BYTES, limit - total + 1))
                if not chunk:
                    return
                total += len(chunk)
                if total > limit:
                    raise Fault("BROKER_RESPONSE_TOO_LARGE", "Provider stream exceeds its byte budget.")
                yield chunk
    except asyncio.IncompleteReadError:
        raise Fault("BROKER_UPSTREAM_PROTOCOL", "Provider response ended before its declared length.") from None
