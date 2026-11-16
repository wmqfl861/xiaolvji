"""Real local HTTP/socketpair protocol tests; no external service or API key."""
import base64
import http.client
import json
import select
import socket
import struct
import threading
import time
import unittest

from xiaolvji.provider_bridge import BridgeServer, validate_runtime


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.inside, self.broker = socket.socketpair()
        self.broker.settimeout(2)
        self.capability = "fixture_capability_" + "x" * 32
        self.runtime = {"version": 1, "capability": self.capability, "api": "openai-responses",
                        "env": {}, "max_request_bytes": 1024, "request_timeout": 2}
        self.server = BridgeServer(self.inside, self.runtime, ("127.0.0.1", 0))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.helpers = []

    def tearDown(self):
        self.server.channel.close()
        active = self.server.active_connection
        if active:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.broker.close()
        for thread in self.helpers:
            thread.join(2)

    def receive(self):
        def exact(size):
            result = b""
            while len(result) < size:
                data = self.broker.recv(size - len(result))
                if not data:
                    raise EOFError()
                result += data
            return result
        size = struct.unpack("!I", exact(4))[0]
        return json.loads(exact(size))

    def send(self, value):
        encoded = json.dumps(value).encode()
        self.broker.sendall(struct.pack("!I", len(encoded)) + encoded)

    def reply(self, request, data=b'{"output":"fixture"}', version=1):
        base = {"v": version, "request_id": request["request_id"]}
        self.send({**base, "type": "response", "status": 200, "headers": {"content-type": "application/json"}})
        if version is True:
            return
        self.send({**base, "type": "data", "data": base64.b64encode(data).decode()})
        self.send({**base, "type": "end"})

    def helper(self, function):
        errors = []
        def call():
            try:
                function()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=call)
        self.helpers.append(thread)
        thread.start()
        return thread, errors

    def post(self, body=b'{"model":"fixture","input":"data"}', path="/v1/responses", headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        actual = {"Authorization": "Bearer " + self.capability, "Content-Type": "application/json"}
        actual.update(headers or {})
        client.request("POST", path, body, actual)
        response = client.getresponse()
        content = response.read()
        client.close()
        return response.status, content

    def test_sequential_requests_stream_without_forwarding_worker_headers(self):
        seen = []
        def provider():
            for _ in range(2):
                request = self.receive()
                seen.append(request)
                self.reply(request)
        thread, errors = self.helper(provider)
        for _ in range(2):
            status, content = self.post(headers={"X-Upstream-URL": "https://unapproved.invalid", "X-API-Key": "worker-choice"})
            self.assertEqual(200, status)
            self.assertEqual({"output": "fixture"}, json.loads(content))
        thread.join(2)
        self.assertEqual([], errors)
        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0]["request_id"], seen[1]["request_id"])
        self.assertEqual({"v", "request_id", "capability", "method", "path", "body"}, set(seen[0]))
        self.assertNotIn("unapproved", json.dumps(seen))

    def test_auth_route_body_framing_fail_before_channel_use(self):
        cases = [({}, "/v1/responses?path=another", b"{}", 404),
                 ({"Authorization": "Bearer wrong"}, "/v1/responses", b"{}", 403),
                 ({"Transfer-Encoding": "chunked"}, "/v1/responses", b"{}", 400),
                 ({"Content-Encoding": "gzip"}, "/v1/responses", b"{}", 400),
                 ({}, "/v1/responses", b'{"model":"a","model":"b"}', 400),
                 ({}, "/v1/responses", b'{"x":NaN}', 400),
                 ({}, "/v1/responses", b"x" * 1025, 413)]
        for headers, path, body, expected in cases:
            with self.subTest(expected=expected, headers=headers, path=path):
                status, _ = self.post(body, path, headers)
                self.assertEqual(expected, status)
                self.assertEqual([], select.select([self.broker], [], [], .01)[0])

    def test_header_bound_rejects_before_any_body_or_channel(self):
        for header in [b"X-Large: " + b"x" * 9000 + b"\r\n",
                       b"".join(b"X-F" + str(i).encode() + b": " + b"x" * 8000 + b"\r\n" for i in range(5))]:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
                client.sendall(b"POST /v1/responses HTTP/1.1\r\n" + header + b"\r\n")
                result = client.recv(4096)
            self.assertIn(b" 431 ", result)
            self.assertEqual([], select.select([self.broker], [], [], .01)[0])

    def test_boolean_protocol_version_closes_channel(self):
        thread, errors = self.helper(lambda: self.reply(self.receive(), version=True))
        status, _ = self.post()
        self.assertEqual(502, status)
        thread.join(2)
        self.assertEqual([], errors)
        self.assertEqual(b"", self.broker.recv(1))

    def test_client_abort_closes_authorized_channel_without_waiting_for_provider(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            body = b'{"model":"fixture","input":"data"}'
            raw = (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"Authorization: Bearer {self.capability}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
            client.sendall(raw)
            self.receive()
        start = time.monotonic()
        self.assertEqual(b"", self.broker.recv(1))
        self.assertLess(time.monotonic() - start, 1)

    def test_sse_terminal_close_drains_matching_end_and_allows_next_request(self):
        closed = threading.Event()
        end_prefix_sent = threading.Event()
        def provider():
            request = self.receive()
            base = {"v": 1, "request_id": request["request_id"]}
            self.send({**base, "type": "response", "status": 200,
                       "headers": {"content-type": "text/event-stream"}})
            event = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            self.send({**base, "type": "data", "data": base64.b64encode(event).decode()})
            # Exercise EOF in the middle of an IPC frame header, not just
            # before an already buffered complete frame.
            ending = json.dumps({**base, "type": "end"}).encode()
            wire = struct.pack("!I", len(ending)) + ending
            self.broker.sendall(wire[:2])
            end_prefix_sent.set()
            self.assertTrue(closed.wait(2))
            time.sleep(.02)
            self.broker.sendall(wire[2:])
            self.reply(self.receive())
        thread, errors = self.helper(provider)
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        body = b'{}'
        client.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                        f"Authorization: Bearer {self.capability}\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        content = b""
        while b'data: {"type":"response.completed"}\n\n' not in content:
            chunk = client.recv(4096)
            self.assertTrue(chunk)
            content += chunk
        self.assertTrue(end_prefix_sent.wait(2))
        client.close()
        closed.set()
        status, content = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {"output": "fixture"})
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(self.server.channel.closed)

    def test_client_close_cannot_reuse_channel_after_mismatched_end(self):
        closed = threading.Event()
        def provider():
            request = self.receive()
            base = {"v": 1, "request_id": request["request_id"]}
            self.send({**base, "type": "response", "status": 200,
                       "headers": {"content-type": "text/event-stream"}})
            self.send({**base, "type": "data", "data": base64.b64encode(b"data: done\n\n").decode()})
            self.assertTrue(closed.wait(2))
            self.send({"v": 1, "request_id": "different", "type": "end"})
        thread, errors = self.helper(provider)
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        client.request("POST", "/v1/responses", b"{}", {"Authorization": "Bearer " + self.capability,
                                                         "Content-Type": "application/json"})
        response = client.getresponse()
        self.assertEqual(response.read(len(b"data: done\n\n")), b"data: done\n\n")
        response.close()
        client.close()
        closed.set()
        self.assertEqual(self.broker.recv(1), b"")
        self.assertEqual(self.post()[0], 503)
        thread.join(2)
        self.assertEqual(errors, [])

    def test_runtime_rejects_secrets_nonprivate_paths_and_unknown_api_shape(self):
        variants = [{"api": []}, {"api": {}}, {"env": {"OPENAI_API_KEY": "real-key-forbidden"}},
                    {"env": {"CODEX_HOME": "/tmp/unapproved"}},
                    {"env": {"XIAOLVJI_PROVIDER_CAPABILITY": "other"}},
                    {"request_timeout": float("nan")}, {"max_request_bytes": True}]
        for change in variants:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate_runtime({**self.runtime, **change})


if __name__ == "__main__":
    unittest.main()
