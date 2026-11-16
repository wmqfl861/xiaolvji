"""Private loopback HTTP adapter for one inherited provider channel.

The supervisor copies this fixed source into the isolated bootstrap. It uses
only the standard library, never imports task code, and never receives an
upstream credential or URL. The outside broker independently validates every
request; this adapter is not the provider authorization boundary.
"""
from __future__ import annotations

import base64
import hmac
from http.client import LineTooLong
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
import uuid


BRIDGE_PORT = 38119
BRIDGE_URL = "http://127.0.0.1:" + str(BRIDGE_PORT)
ROUTES = {"openai-responses": "/v1/responses",
          "openai-chat-completions": "/v1/chat/completions",
          "anthropic-messages": "/v1/messages"}
MAX_FRAME = 16 * 1024 * 1024 + 65536
MAX_BODY = 16 * 1024 * 1024
MAX_HEADER = 32 * 1024
CLIENT_EOF_GRACE = .15


class BridgeError(Exception):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("non-finite number")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_invalid_number)


def validate_runtime(value):
    """Validate only the synthetic inside-boundary launch envelope.

    Host renderers supply this envelope. Upstream destinations/credentials and
    the authoritative model/quality constraints do not belong here.
    """
    required = {"version", "capability", "api", "env", "max_request_bytes", "request_timeout"}
    if not isinstance(value, dict) or set(value) != required or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid provider runtime envelope")
    if not isinstance(value["api"], str) or value["api"] not in ROUTES or not isinstance(value["capability"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value["capability"]):
        raise ValueError("invalid provider capability or API")
    if type(value["max_request_bytes"]) is not int or not 1 <= value["max_request_bytes"] <= MAX_BODY:
        raise ValueError("invalid request bound")
    if type(value["request_timeout"]) not in (int, float) or not 0 < value["request_timeout"] <= 3600:
        raise ValueError("invalid request deadline")
    fixed_paths = {"CODEX_HOME": "/home/xiaolvji/.codex",
                   "ZCODE_DATA_BASE_DIR": "/home/xiaolvji",
                   "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": "/home/xiaolvji/.zcode/v2/provider_config.json",
                   "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": "/home/xiaolvji/.zcode/v2/xiaolvji_builtin_config.json"}
    allowed = {"XIAOLVJI_PROVIDER_CAPABILITY", *fixed_paths}
    env = value["env"]
    if not isinstance(env, dict) or set(env) - allowed:
        raise ValueError("unsupported host environment override")
    for key, item in env.items():
        if not isinstance(item, str) or "\x00" in item or len(item) > 4096:
            raise ValueError("invalid host environment value")
        if key == "XIAOLVJI_PROVIDER_CAPABILITY":
            if not hmac.compare_digest(item, value["capability"]):
                raise ValueError("capability mismatch")
        elif item != fixed_paths[key]:
            raise ValueError("host configuration must remain in private HOME")
    return _json(json.dumps(value, allow_nan=False))


class Channel:
    """Exactly one sequential stream, invalidated by any framing failure."""
    def __init__(self, endpoint, runtime):
        self.endpoint = endpoint
        self.runtime = validate_runtime(runtime)
        self.lock = threading.Lock()
        self.closed = False
        self.client_eof_deadline = None

    def client_disconnected(self):
        if self.client_eof_deadline is None:
            self.client_eof_deadline = time.monotonic() + CLIENT_EOF_GRACE

    def close(self):
        self.closed = True
        try:
            self.endpoint.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.endpoint.close()

    def _read(self, size, client, deadline):
        result = bytearray()
        while len(result) < size:
            effective_deadline = min(deadline, self.client_eof_deadline) if self.client_eof_deadline is not None else deadline
            remaining = effective_deadline - time.monotonic()
            if remaining <= 0 or self.closed:
                raise BridgeError("provider channel deadline")
            watched = [self.endpoint] if self.client_eof_deadline is not None else [self.endpoint, client]
            readable, _, _ = select.select(watched, [], [], min(remaining, .25))
            if client in readable:
                # SSE clients may close after their terminal event, before the
                # last HTTP chunk. Drain only this response for at most 150 ms.
                # Reuse requires a valid broker end; otherwise close the grant.
                # Additional pipelined bytes remain an immediate error.
                try:
                    extra = client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                except (ConnectionResetError, BrokenPipeError):
                    extra = b""
                if extra:
                    raise BridgeError("client pipelined")
                self.client_disconnected()
            if self.endpoint in readable:
                part = self.endpoint.recv(size - len(result))
                if not part:
                    raise BridgeError("provider channel closed")
                result.extend(part)
        return bytes(result)

    def frame(self, client, deadline, request_id):
        size = struct.unpack("!I", self._read(4, client, deadline))[0]
        if not 0 < size <= MAX_FRAME:
            raise BridgeError("provider frame exceeds bound")
        value = _json(self._read(size, client, deadline))
        if not isinstance(value, dict) or type(value.get("v")) is not int or value.get("v") != 1 or value.get("request_id") != request_id:
            raise BridgeError("provider frame identity mismatch")
        return value

    def send(self, body):
        data = json.dumps(body, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_FRAME:
            raise BridgeError("request frame exceeds bound")
        self.endpoint.settimeout(min(10, self.runtime["request_timeout"]))
        self.endpoint.sendall(struct.pack("!I", len(data)) + data)
        self.endpoint.settimeout(None)


class BridgeServer(HTTPServer):
    request_queue_size = 4
    allow_reuse_address = False

    def __init__(self, endpoint, runtime, address=("127.0.0.1", BRIDGE_PORT)):
        self.channel = Channel(endpoint, runtime)
        self.active_connection = None
        super().__init__(address, BridgeHandler)

    def handle_error(self, request, client_address):
        # Never dump a traceback, headers, task bodies or synthetic capability.
        pass


class _HeaderReader:
    """Bound bytes before http.client's email-header parser allocates them."""
    def __init__(self, source):
        self.source = source
        self.remaining = MAX_HEADER

    def readline(self, limit=-1):
        maximum = min(8193, self.remaining + 1)
        if limit >= 0:
            maximum = min(maximum, limit)
        line = self.source.readline(maximum)
        if len(line) > 8192 or len(line) > self.remaining:
            raise LineTooLong("scoped provider headers")
        self.remaining -= len(line)
        return line


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xiaolvji"
    sys_version = ""
    timeout = 10

    def parse_request(self):
        if len(self.raw_requestline) > 8192:
            self.requestline = ""
            self.request_version = "HTTP/1.1"
            self.command = None
            self._error(414)
            return False
        source = self.rfile
        self.rfile = _HeaderReader(source)
        try:
            return super().parse_request()
        finally:
            self.rfile = source

    def setup(self):
        super().setup()
        self.server.active_connection = self.connection
        self._deadline_timer = None
        self._deadline(10)

    def _deadline(self, seconds):
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        def expire():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._deadline_timer = threading.Timer(seconds, expire)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def finish(self):
        try:
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
            super().finish()
        finally:
            self.server.active_connection = None

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._error(code)

    def _error(self, code):
        body = b'{"error":{"message":"Scoped provider request rejected"}}'
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _stream_write(self, data):
        channel = self.server.channel
        if channel.client_eof_deadline is not None:
            return
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            channel.client_disconnected()

    def do_POST(self):
        self.close_connection = True
        channel = self.server.channel
        runtime = channel.runtime
        started_response = False
        forwarded = False
        try:
            if self.request_version not in {"HTTP/1.0", "HTTP/1.1"} or self.path != ROUTES[runtime["api"]]:
                self._error(404)
                return
            # Reject ambiguous HTTP framing and all worker-selected upstream
            # metadata. The broker synthesizes its own trusted HTTP headers.
            headers = {}
            count = 0
            for key, value in self.headers.items():
                count += len(key) + len(value) + 4
                key = key.lower()
                if key in headers or "\r" in value or "\n" in value:
                    self._error(400)
                    return
                headers[key] = value
            if count > MAX_HEADER or "transfer-encoding" in headers or headers.get("content-encoding", "identity") != "identity":
                self._error(400)
                return
            if headers.get("host") != "127.0.0.1:" + str(self.server.server_address[1]):
                self._error(400)
                return
            key = "x-api-key" if runtime["api"] == "anthropic-messages" else "authorization"
            expected = runtime["capability"] if key == "x-api-key" else "Bearer " + runtime["capability"]
            if not hmac.compare_digest(headers.get(key, ""), expected):
                self._error(403)
                return
            length = headers.get("content-length", "")
            if not re.fullmatch(r"[0-9]{1,8}", length) or not 0 < int(length) <= runtime["max_request_bytes"]:
                self._error(413)
                return
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._error(415)
                return
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise BridgeError("incomplete client body")
            body = _json(body)
            if not isinstance(body, dict):
                raise BridgeError("body must be an object")
            if channel.closed or not channel.lock.acquire(blocking=False):
                self._error(503)
                return
            try:
                channel.client_eof_deadline = None
                request_id = uuid.uuid4().hex
                deadline = time.monotonic() + runtime["request_timeout"] + 2
                self._deadline(runtime["request_timeout"] + 2)
                forwarded = True
                channel.send({"v": 1, "request_id": request_id, "capability": runtime["capability"],
                              "method": "POST", "path": self.path, "body": body})
                response = channel.frame(self.connection, deadline, request_id)
                if response.get("type") == "error":
                    self._error(502)
                    return
                if set(response) != {"v", "type", "request_id", "status", "headers"} or response["type"] != "response" or type(response["status"]) is not int or not 200 <= response["status"] < 300:
                    raise BridgeError("invalid response header frame")
                content_type = response["headers"].get("content-type") if isinstance(response["headers"], dict) else None
                if content_type not in {"application/json", "text/event-stream"} or set(response["headers"]) != {"content-type"}:
                    raise BridgeError("invalid response content type")
                self.send_response_only(response["status"])
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                try:
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    channel.client_disconnected()
                started_response = True
                total = 0
                while True:
                    frame = channel.frame(self.connection, deadline, request_id)
                    if frame.get("type") == "end":
                        if set(frame) != {"v", "type", "request_id"}:
                            raise BridgeError("invalid end frame")
                        self._stream_write(b"0\r\n\r\n")
                        return
                    if frame.get("type") != "data" or set(frame) != {"v", "type", "request_id", "data"}:
                        raise BridgeError("provider stream interrupted")
                    chunk = base64.b64decode(frame["data"], validate=True)
                    total += len(chunk)
                    if not chunk or len(chunk) > 1024 * 1024 or total > 64 * 1024 * 1024:
                        raise BridgeError("provider stream exceeds bridge bound")
                    self._stream_write(format(len(chunk), "x").encode() + b"\r\n" + chunk + b"\r\n")
            finally:
                channel.lock.release()
        except (OSError, ValueError, TypeError, KeyError, RecursionError, BridgeError):
            if forwarded:
                channel.close()
            if not started_response:
                try:
                    self._error(502 if forwarded else 400)
                except OSError:
                    pass


def run_bridge(argv, runtime, provider_fd=3):
    """Run the unchanged host command with a private HTTP adapter beside it."""
    runtime = validate_runtime(runtime)
    endpoint = socket.socket(fileno=provider_fd)
    endpoint.set_inheritable(False)
    server = BridgeServer(endpoint, runtime)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    try:
        environment = dict(os.environ)
        environment.update(runtime["env"])
        # The model CLI receives only loopback HTTP plus its synthetic token.
        # It never inherits the actual channel descriptor or a broker secret.
        child = subprocess.Popen(argv, env=environment, close_fds=True)
        return child.wait()
    finally:
        server.channel.close()
        active_connection = server.active_connection
        if active_connection is not None:
            try:
                active_connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
