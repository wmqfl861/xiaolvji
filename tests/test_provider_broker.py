"""Synthetic-only provider fixtures; no real credentials or external providers."""
import asyncio
import array
import base64
import builtins
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, canonical_json
from xiaolvji.provider_broker import (
    API_ROUTES, ProviderBroker, ProviderProfile, _Redactor,
    normalize_provider_profile, read_frame, write_frame,
)


SECRET = "synthetic-upstream-key-DO-NOT-REVEAL"


def profile(**changes):
    values = {"profile_id": "fixture", "api": "openai-responses",
              "upstream_url": "https://fixture.invalid/registered/responses",
              "model": "fixture-model", "request_constraints": {"reasoning": {"effort": "high"}},
              "request_timeout": 1, "max_requests": 8}
    values.update(changes)
    return ProviderProfile(**values)


class ProfileTests(unittest.TestCase):
    def test_closed_profile_and_explicit_tls_destination(self):
        original = profile().to_dict()
        self.assertEqual(original, normalize_provider_profile(original).to_dict())
        for extra in ({"api_key": SECRET}, {"allow_http": True}, {"headers": {}}):
            with self.subTest(extra=tuple(extra)), self.assertRaises(Fault):
                normalize_provider_profile(dict(original, **extra))
        for url in ("http://fixture.invalid/responses", "https://user:pass@fixture.invalid/v1/responses",
                    "https://fixture.invalid/v1/responses?key=secret", "https://fixture.invalid/v1/responses#frag",
                    "https://fixture.invalid", "https://fixture.invalid/a\r\nInjected:yes", "https://fixture.invalid:0/a"):
            with self.subTest(url=url), self.assertRaises(Fault):
                profile(upstream_url=url)

    def test_limits_reject_nonfinite_and_boolean_integers(self):
        for key, value in (("max_requests", True), ("max_requests", 0), ("max_concurrency", 65),
                           ("request_timeout", float("nan")), ("request_timeout", float("inf")),
                           ("max_request_bytes", 1), ("max_response_bytes", 0)):
            with self.subTest(key=key, value=value), self.assertRaises(Fault):
                profile(**{key: value})

    def test_constraints_have_an_immutable_canonical_snapshot(self):
        supplied = {"reasoning": {"effort": "high"}}
        value = profile(request_constraints=supplied)
        supplied["reasoning"]["effort"] = "low"
        value.request_constraints["reasoning"]["effort"] = "low"
        returned = value.to_dict()
        returned["request_constraints"]["reasoning"]["effort"] = "low"
        self.assertEqual("high", value.to_dict()["request_constraints"]["reasoning"]["effort"])

    def test_credential_redaction_handles_every_split_and_json_escaping(self):
        secret = 'test-secret-with-quote-"-and-backslash-\\'
        text = b'prefix:' + secret.encode() + b":" + canonical_json(secret)[1:-1].encode() + b':suffix'
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                redactor = _Redactor([secret])
                result = redactor.feed(text[:split]) + redactor.feed(text[split:]) + redactor.feed(b"", final=True)
                self.assertEqual(b"prefix:[REDACTED]:[REDACTED]:suffix", result)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.admission = {}
        self.received = []
        self.upstream_writers = set()
        self.upstream_tasks = set()
        self.response = b'{"output":"synthetic success"}'
        self.response_mode = "normal"
        self.upstream_entered = asyncio.Event()
        self.upstream_closed = asyncio.Event()
        self.release_response = asyncio.Event()
        self.server = await asyncio.start_server(self._upstream, "127.0.0.1", 0, family=socket.AF_INET)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []
        self.brokers = []
        self.sequence = 0
        self.broker = await self.make_broker()

    async def asyncTearDown(self):
        for _, writer, _ in self.clients:
            writer.close()
        for _, writer, _ in self.clients:
            await writer.wait_closed()
        for broker in self.brokers:
            await broker.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.upstream_writers):
            writer.close()
        for task in list(self.upstream_tasks):
            task.cancel()
        if self.upstream_tasks:
            await asyncio.gather(*self.upstream_tasks, return_exceptions=True)
        self.temp.cleanup()

    def check_admission(self, binding, phase):
        item = self.admission.get(binding["attempt_id"])
        return (item is not None and item["binding"] == binding
                and item["phase"] == ("starting" if phase == "grant" else "running"))

    async def make_broker(self, *, chosen=None, directory="broker", global_concurrency=2):
        chosen = chosen or profile()
        broker = ProviderBroker(self.root / directory, {chosen.profile_id: chosen},
                                {chosen.profile_id: SECRET}, self.check_admission, global_concurrency)
        # Private programmatic fixture seam: production profiles remain HTTPS,
        # and production code has no plaintext/config/environment fallback.
        async def local_fixture(_profile):
            return await asyncio.open_connection("127.0.0.1", self.port, family=socket.AF_INET)
        broker._connect_upstream = local_fixture
        await broker.start()
        self.brokers.append(broker)
        return broker

    async def client(self, name="attempt", *, broker=None, generation=1, ttl=30, run=True):
        broker = broker or self.broker
        binding = {"attempt_id": name, "generation": generation, "launch_key": f"launch:{name}:{generation}"}
        self.admission[name] = {"binding": binding, "phase": "starting"}
        grant = await broker.grant(binding, "fixture", ttl_seconds=ttl)
        # Model the real supervisor handoff: a duplicated descriptor survives
        # closing the controller's worker endpoint. There is no listener.
        transferred = socket.socket(fileno=os.dup(grant.worker_socket.fileno()))
        grant.close_worker_endpoint()
        reader, writer = await asyncio.open_connection(sock=transferred)
        if run:
            self.admission[name]["phase"] = "running"
        session = reader, writer, grant
        self.clients.append(session)
        return session

    def request_frame(self, client, *, body=None, request_id=None, **fields):
        self.sequence += 1
        payload = {"model": "fixture-model", "reasoning": {"effort": "high"}, "input": "hello",
                   "stream": True, "store": False} if body is None else body
        frame = {"v": 1, "request_id": request_id or f"request-{self.sequence}",
                 "capability": client[2].capability, "method": "POST", "path": "/v1/responses", "body": payload}
        frame.update(fields)
        return frame

    async def response_frames(self, client):
        frames = []
        async with asyncio.timeout(3):
            while True:
                frame = await read_frame(client[0])
                frames.append(frame)
                if frame["type"] in {"end", "error"}:
                    return frames

    async def request(self, client, **kwargs):
        await write_frame(client[1], self.request_frame(client, **kwargs))
        return await self.response_frames(client)

    async def _upstream(self, reader, writer):
        task = asyncio.current_task()
        self.upstream_tasks.add(task)
        self.upstream_writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("ascii").split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
            body = await reader.readexactly(int(headers["content-length"]))
            self.received.append({"start": lines[0], "headers": headers, "body": json.loads(body), "raw": raw + body})
            self.upstream_entered.set()
            mode = self.response_mode
            if mode == "hold":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                waiter = asyncio.create_task(self.release_response.wait())
                disconnect = asyncio.create_task(reader.read())
                try:
                    done, _ = await asyncio.wait({waiter, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnect in done:
                        self.upstream_closed.set()
                        return
                finally:
                    waiter.cancel()
                    disconnect.cancel()
                    await asyncio.gather(waiter, disconnect, return_exceptions=True)
                writer.write(b"2\r\n{}\r\n0\r\n\r\n")
            elif mode == "disconnect":
                return
            elif mode == "reject":
                writer.write(f"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {len(SECRET)}\r\n\r\n{SECRET}".encode())
            elif mode == "redirect":
                writer.write(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: https://other.invalid/\r\nContent-Length: 0\r\n\r\n")
            elif mode == "duplicate":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
            elif mode == "ambiguous":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}")
            elif mode == "compressed":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\n{}")
            elif mode == "too_many_headers":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + b"Ignored: x\r\n" * 65 + b"\r\n{}")
            elif mode == "truncated":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{}")
            elif mode == "badchunk":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n2;evil=metadata\r\n{}\r\n0\r\n\r\n")
            elif mode in {"chunked", "one_byte_chunks"}:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nTransfer-Encoding: chunked\r\nX-Debug-Credential: " + SECRET.encode() + b"\r\n\r\n")
                step = 1 if mode == "one_byte_chunks" else 11
                for start in range(0, len(self.response), step):
                    chunk = self.response[start:start + step]
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                writer.write(b"0\r\nIgnored-Trailer: discarded\r\n\r\n")
            elif mode == "eof":
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + self.response)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(self.response)).encode() + b"\r\n\r\n" + self.response)
            await writer.drain()
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.upstream_writers.discard(writer)
            self.upstream_tasks.discard(task)

    @staticmethod
    def data(frames):
        return b"".join(base64.b64decode(item["data"]) for item in frames if item["type"] == "data")

    async def test_pinned_endpoint_secret_stays_upstream_and_channel_is_reusable(self):
        client = await self.client()
        frames = await self.request(client)
        self.assertEqual("end", frames[-1]["type"])
        self.assertEqual(self.response, self.data(frames))
        self.assertEqual("POST /registered/responses HTTP/1.1", self.received[0]["start"])
        self.assertEqual("bearer " + SECRET.lower(), self.received[0]["headers"]["authorization"])
        self.assertNotIn(client[2].capability.encode(), self.received[0]["raw"])
        self.assertEqual("end", (await self.request(client))[-1]["type"])
        status = self.broker.status()
        self.assertEqual(2, status["grants"][0]["requests_consumed"])
        self.assertEqual(0, status["global_in_flight"])
        self.assertEqual("unknown", status["remote_processing_after_disconnect"])
        for path in (self.root / "broker").iterdir():
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())
                self.assertNotIn(client[2].capability.encode(), path.read_bytes())
        self.assertNotIn(SECRET, repr(client[2]))
        self.assertNotIn(client[2].capability, repr(client[2]))

    async def test_model_quality_and_remote_authority_rejections_consume_no_budget(self):
        client = await self.client()
        baseline = self.request_frame(client)["body"]
        bad_bodies = [dict(baseline, model="other"), dict(baseline, reasoning={"effort": "low"}),
                      {k: v for k, v in baseline.items() if k != "reasoning"},
                      dict(baseline, background=True), dict(baseline, store=True),
                      dict(baseline, tools=[{"type": "web_search"}]),
                      dict(baseline, tools=[{"type": ["function"]}]),
                      dict(baseline, input=[{"type": "input_image", "image_url": "https://other.invalid/x"}])]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.assertEqual("error", (await self.request(client, body=body))[-1]["type"])
        for fields in ({"path": "https://other.invalid/"}, {"method": "GET"}, {"path": "/v1/models"}):
            self.assertEqual("BROKER_REQUEST_REJECTED", (await self.request(client, **fields))[-1]["code"])
        self.assertEqual([], self.received)
        self.assertEqual(0, self.broker.status()["grants"][0]["requests_consumed"])
        allowed = dict(baseline, reasoning={"effort": "high", "summary": "auto"}, tools=[{"type": "function", "name": "read_file"}])
        self.assertEqual("end", (await self.request(client, body=allowed))[-1]["type"])

    async def test_native_client_metadata_is_preserved_as_bounded_opaque_strings(self):
        client = await self.client()
        metadata = {"thread_id": "synthetic-thread", "x-codex-turn-metadata":
                    '{"sandbox_mode":"workspace-write","model":"fixture-model"}'}
        body = dict(self.request_frame(client)["body"], client_metadata=metadata,
                    tools=[{"type": "function", "name": "exec_command"}])
        self.assertEqual("end", (await self.request(client, body=body))[-1]["type"])
        self.assertEqual(metadata, self.received[0]["body"]["client_metadata"])
        self.assertEqual(1, self.broker.status()["grants"][0]["requests_consumed"])

    async def test_native_metadata_invalid_shapes_and_bounds_consume_no_budget(self):
        client = await self.client()
        baseline = self.request_frame(client)["body"]
        cases = [None, [], "metadata", {"nested": {"model": "fixture-model"}},
                 {"flag": True}, {"number": 1}, {"null": None}, {"": "empty key"},
                 {"x" * 129: "long key"}, {"x": "y" * 4097},
                 {str(index): "v" for index in range(33)},
                 {str(index): "v" * 4096 for index in range(9)},
                 {"x": "\u00e9" * 2049}]
        for metadata in cases:
            with self.subTest(metadata_type=type(metadata).__name__):
                result = await self.request(client, body=dict(baseline, client_metadata=metadata))
                self.assertEqual("BROKER_REQUEST_REJECTED", result[-1]["code"])
        self.assertEqual([], self.received)
        self.assertEqual(0, self.broker.status()["grants"][0]["requests_consumed"])
        # Metadata never controls the actual routing, model, quality, or tools.
        metadata = {"model": "fixture-model", "reasoning": '{"effort":"high"}',
                    "allow_remote_tools": "true"}
        for extra in ({"model": "other"}, {"reasoning": {"effort": "low"}},
                      {"tools": [{"type": "web_search"}]}):
            result = await self.request(client, body=dict(baseline, client_metadata=metadata, **extra))
            self.assertEqual("error", result[-1]["type"])
        self.assertEqual([], self.received)
        self.assertEqual(0, self.broker.status()["grants"][0]["requests_consumed"])

    async def test_grant_requires_exact_current_binding_and_requests_require_running(self):
        binding = {"attempt_id": "unadmitted", "generation": 1, "launch_key": "launch:unadmitted:1"}
        with self.assertRaises(Fault) as caught:
            await self.broker.grant(binding, "fixture")
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        client = await self.client(run=False)
        self.assertEqual("BROKER_ADMISSION_DENIED", (await self.request(client))[-1]["code"])
        self.assertEqual([], self.received)
        self.admission["attempt"]["phase"] = "running"
        self.assertEqual("end", (await self.request(client))[-1]["type"])

    async def test_capability_is_attempt_specific_and_revokes_channel_on_mismatch(self):
        first, second = await self.client("first"), await self.client("second")
        response = await self.request(first, capability=second[2].capability)
        self.assertEqual("BROKER_UNAUTHORIZED", response[-1]["code"])
        self.assertEqual(b"", await asyncio.wait_for(first[0].read(), 1))
        self.assertEqual([], self.received)
        self.assertEqual("end", (await self.request(second))[-1]["type"])

    async def test_request_replay_and_budget_fail_closed(self):
        broker = await self.make_broker(chosen=profile(max_requests=1), directory="budget")
        client = await self.client(broker=broker)
        self.assertEqual("end", (await self.request(client, request_id="once"))[-1]["type"])
        self.assertEqual("BROKER_REQUEST_REPLAY", (await self.request(client, request_id="once"))[-1]["code"])
        self.assertEqual("BROKER_BUDGET_EXHAUSTED", (await self.request(client, request_id="different"))[-1]["code"])
        self.assertEqual(1, len(self.received))
        self.assertEqual(1, broker.status()["grants"][0]["requests_consumed"])

    async def test_failed_connect_still_consumes_budget_before_attempting_upstream(self):
        broker = await self.make_broker(chosen=profile(max_requests=1), directory="connect-failure")
        client = await self.client(broker=broker)
        observed = []
        async def refused(_profile):
            observed.append(broker.status())
            raise OSError(SECRET)
        broker._connect_upstream = refused
        response = await self.request(client)
        self.assertEqual("BROKER_UPSTREAM_ERROR", response[-1]["code"])
        self.assertNotIn(SECRET, json.dumps(response))
        self.assertEqual(1, observed[0]["grants"][0]["requests_consumed"])
        self.assertEqual("IN_FLIGHT", observed[0]["requests"][0]["state"])
        self.assertEqual("BROKER_BUDGET_EXHAUSTED", (await self.request(client))[-1]["code"])

    async def test_local_global_capacity_returns_busy_and_refills_after_completion(self):
        broker = await self.make_broker(chosen=profile(max_concurrency=2), directory="global", global_concurrency=1)
        first, second = await self.client("first", broker=broker), await self.client("second", broker=broker)
        self.response_mode = "hold"
        await write_frame(first[1], self.request_frame(first))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        self.assertEqual("BROKER_BUSY", (await self.request(second))[-1]["code"])
        self.assertEqual(1, broker.status()["global_in_flight"])
        self.release_response.set()
        self.assertEqual("end", (await self.response_frames(first))[-1]["type"])
        self.response_mode = "normal"
        self.assertEqual("end", (await self.request(second))[-1]["type"])
        self.assertEqual(0, broker.status()["global_in_flight"])

    async def test_local_provider_capacity_is_independent_of_global_capacity(self):
        broker = await self.make_broker(chosen=profile(max_concurrency=1), directory="provider", global_concurrency=2)
        first, second = await self.client("first", broker=broker), await self.client("second", broker=broker)
        self.response_mode = "hold"
        await write_frame(first[1], self.request_frame(first))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        self.assertEqual("BROKER_BUSY", (await self.request(second))[-1]["code"])
        self.release_response.set()
        self.assertEqual("end", (await self.response_frames(first))[-1]["type"])

    async def test_explicit_revoke_closes_active_upstream_and_keeps_consumed_budget(self):
        client = await self.client()
        self.response_mode = "hold"
        await write_frame(client[1], self.request_frame(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        await self.broker.revoke("attempt", 1)
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        await asyncio.wait_for(client[0].read(), 1)
        status = self.broker.status()
        self.assertEqual(0, status["global_in_flight"])
        self.assertEqual("REVOKED", status["grants"][0]["status"])
        self.assertEqual("revoked", status["grants"][0]["reason"])
        self.assertEqual(1, status["grants"][0]["requests_consumed"])
        self.assertEqual("LOCAL_ENDED", status["requests"][0]["state"])

    async def test_concurrent_revoke_does_not_interrupt_accounting_cleanup(self):
        client = await self.client()
        self.response_mode = "hold"
        pending_request = asyncio.create_task(self.request(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        first = asyncio.create_task(self.broker.revoke("attempt", 1))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.broker.revoke("attempt", 1))
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        await asyncio.gather(pending_request, return_exceptions=True)
        status = self.broker.status()
        self.assertEqual(0, status["global_in_flight"])
        self.assertEqual(0, status["provider_in_flight"]["fixture"])
        self.assertEqual("LOCAL_ENDED", status["requests"][0]["state"])
        self.assertEqual("local_cancelled", status["requests"][0]["outcome"])

    async def test_cancelled_revoke_caller_does_not_cancel_owned_cleanup_twice(self):
        client = await self.client()
        self.response_mode = "hold"
        pending_request = asyncio.create_task(self.request(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        revocation = asyncio.create_task(self.broker.revoke("attempt", 1))
        await asyncio.sleep(0)
        revocation.cancel()
        await asyncio.gather(revocation, return_exceptions=True)
        await asyncio.wait_for(self.broker.revoke("attempt", 1), 1)
        await asyncio.gather(pending_request, return_exceptions=True)
        status = self.broker.status()
        self.assertEqual(0, status["global_in_flight"])
        self.assertEqual("LOCAL_ENDED", status["requests"][0]["state"])

    async def test_cancelled_close_continues_owned_cleanup_and_is_awaitable_again(self):
        client = await self.client()
        self.response_mode = "hold"
        pending_request = asyncio.create_task(self.request(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        closing = asyncio.create_task(self.broker.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        await asyncio.wait_for(self.broker.close(), 1)
        await asyncio.gather(pending_request, return_exceptions=True)
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        self.assertEqual(0, self.broker._active)
        self.assertIsNone(self.broker._db)
        self.assertEqual({}, self.broker._credentials)
        db = sqlite3.connect(self.root / "broker/journal.sqlite3")
        try:
            self.assertEqual("LOCAL_ENDED", db.execute("SELECT state FROM requests").fetchone()[0])
            self.assertEqual("REVOKED", db.execute("SELECT status FROM grants").fetchone()[0])
        finally:
            db.close()

    async def test_admission_watchdog_closes_stream_without_another_request(self):
        client = await self.client()
        self.response_mode = "hold"
        await write_frame(client[1], self.request_frame(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        self.admission["attempt"]["phase"] = "cancelled"
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        await asyncio.wait_for(client[0].read(), 1)
        self.assertEqual("admission_lost", self.broker.status()["grants"][0]["reason"])
        self.assertEqual(0, self.broker.status()["global_in_flight"])

    async def test_request_timeout_closes_upstream_and_does_not_refund(self):
        broker = await self.make_broker(chosen=profile(request_timeout=0.1), directory="timeout")
        client = await self.client(broker=broker)
        self.response_mode = "hold"
        frames = await self.request(client)
        self.assertEqual("BROKER_REQUEST_TIMEOUT", frames[-1]["code"])
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        self.assertEqual(1, broker.status()["grants"][0]["requests_consumed"])
        self.assertEqual(0, broker.status()["global_in_flight"])

    async def test_grant_expiry_closes_idle_worker_descriptor(self):
        client = await self.client(ttl=0.08)
        self.assertEqual(b"", await asyncio.wait_for(client[0].read(), 1))
        self.assertEqual("expired", self.broker.status()["grants"][0]["reason"])

    async def test_worker_disconnect_stops_inflight_request(self):
        client = await self.client()
        self.response_mode = "hold"
        await write_frame(client[1], self.request_frame(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        client[1].close()
        await client[1].wait_closed()
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        for _ in range(50):
            if self.broker.status()["global_in_flight"] == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(0, self.broker.status()["global_in_flight"])

    async def test_pipelining_is_rejected_and_aborts_local_upstream(self):
        client = await self.client()
        self.response_mode = "hold"
        await write_frame(client[1], self.request_frame(client))
        await asyncio.wait_for(self.upstream_entered.wait(), 1)
        await write_frame(client[1], self.request_frame(client))
        frames = await self.response_frames(client)
        self.assertEqual("BROKER_PROTOCOL_ERROR", frames[-1]["code"])
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        self.assertEqual(1, len(self.received))

    async def test_malformed_duplicate_and_oversized_frames_close_channel(self):
        for index, raw in enumerate((b'{"v":1,"v":1}', b"[]", b'{"v":NaN}')):
            client = await self.client(f"bad-{index}")
            client[1].write(struct.pack("!I", len(raw)) + raw)
            await client[1].drain()
            self.assertEqual("BROKER_PROTOCOL_ERROR", (await self.response_frames(client))[-1]["code"])
        client = await self.client("oversized")
        client[1].write(struct.pack("!I", 2**32 - 1))
        await client[1].drain()
        self.assertEqual("BROKER_PROTOCOL_ERROR", (await self.response_frames(client))[-1]["code"])
        self.assertEqual([], self.received)

    async def test_request_body_limit_is_enforced_before_budget(self):
        broker = await self.make_broker(chosen=profile(max_request_bytes=128), directory="request-limit")
        client = await self.client(broker=broker)
        body = self.request_frame(client)["body"]
        body["input"] = "x" * 200
        self.assertEqual("BROKER_REQUEST_TOO_LARGE", (await self.request(client, body=body))[-1]["code"])
        self.assertEqual([], self.received)
        self.assertEqual(0, broker.status()["grants"][0]["requests_consumed"])

    async def test_response_limits_cover_content_length_chunked_and_eof(self):
        broker = await self.make_broker(chosen=profile(max_response_bytes=16), directory="response-limit")
        client = await self.client(broker=broker)
        self.response = b"x" * 17
        for mode in ("normal", "chunked", "eof"):
            with self.subTest(mode=mode):
                self.response_mode = mode
                self.assertEqual("BROKER_RESPONSE_TOO_LARGE", (await self.request(client))[-1]["code"])
        self.assertEqual(3, broker.status()["grants"][0]["requests_consumed"])

    async def test_http_framing_redirect_and_compression_are_fail_closed(self):
        broker = await self.make_broker(chosen=profile(max_requests=12), directory="http")
        client = await self.client(broker=broker)
        for mode in ("redirect", "duplicate", "ambiguous", "compressed", "too_many_headers", "truncated", "badchunk", "disconnect"):
            with self.subTest(mode=mode):
                self.response_mode = mode
                response = await self.request(client)
                self.assertEqual("error", response[-1]["type"])
        self.assertEqual(8, len(self.received))
        self.assertEqual(8, broker.status()["grants"][0]["requests_consumed"])

    async def test_response_secret_redaction_across_chunk_boundaries_and_headers(self):
        client = await self.client()
        self.response_mode = "one_byte_chunks"
        self.response = b'data: {"value":"' + SECRET.encode() + b'"}\n\n'
        frames = await self.request(client)
        self.assertEqual("end", frames[-1]["type"])
        self.assertEqual(b'data: {"value":"[REDACTED]"}\n\n', self.data(frames))
        self.assertNotIn(SECRET, json.dumps(frames))
        self.assertEqual({"content-type": "text/event-stream"}, frames[0]["headers"])
        self.response_mode = "reject"
        response = await self.request(client)
        self.assertEqual("BROKER_UPSTREAM_REJECTED", response[-1]["code"])
        self.assertNotIn(SECRET, json.dumps(response))

    async def test_chat_and_anthropic_routes_use_only_their_registered_protocol(self):
        cases = (("openai-chat-completions", {"reasoning_effort": "high"}),
                 ("anthropic-messages", {"thinking": {"type": "enabled", "budget_tokens": 1024}, "max_tokens": 2048}))
        for index, (api, constraints) in enumerate(cases):
            broker = await self.make_broker(chosen=profile(api=api, request_constraints=constraints,
                                                         upstream_url="https://fixture.invalid/exact/" + str(index)), directory="api-" + str(index))
            client = await self.client("api-" + str(index), broker=broker)
            body = {"model": "fixture-model", "messages": [{"role": "user", "content": "hello"}], **constraints}
            frames = await self.request(client, body=body, path=API_ROUTES[api])
            self.assertEqual("end", frames[-1]["type"])
            headers = self.received[-1]["headers"]
            if api == "anthropic-messages":
                self.assertEqual(SECRET.lower(), headers["x-api-key"])
                self.assertEqual("2023-06-01", headers["anthropic-version"])
                self.assertNotIn("authorization", headers)
            else:
                self.assertIn("authorization", headers)
                self.assertNotIn("x-api-key", headers)

    async def test_journal_single_owner_and_generation_never_reissued(self):
        client = await self.client()
        duplicate = ProviderBroker(self.root / "broker", {"fixture": profile()}, {"fixture": SECRET}, self.check_admission)
        with self.assertRaises(Fault) as caught:
            await duplicate.start()
        self.assertEqual("BROKER_ALREADY_OWNED", caught.exception.code)
        self.admission["attempt"]["phase"] = "starting"
        with self.assertRaises(Fault) as caught:
            await self.broker.grant(client[2].binding, "fixture")
        self.assertEqual("BROKER_GRANT_REUSED", caught.exception.code)
        await self.broker.close()
        restarted = await self.make_broker(directory="broker")
        with self.assertRaises(Fault) as caught:
            await restarted.grant(client[2].binding, "fixture")
        self.assertEqual("BROKER_GRANT_REUSED", caught.exception.code)
        next_client = await self.client(generation=2, broker=restarted)
        self.assertNotEqual(client[2].capability, next_client[2].capability)
        self.assertEqual("end", (await self.request(next_client))[-1]["type"])

    async def test_more_than_256_immediately_revoked_grants_recycle_live_capacity(self):
        for index in range(300):
            name = f"rolling-{index}"
            binding = {"attempt_id": name, "generation": 1, "launch_key": f"launch:{name}:1"}
            self.admission[name] = {"binding": binding, "phase": "starting"}
            grant = await self.broker.grant(binding, "fixture")
            try:
                # Deliberately no yield after grant: revoke cancels the newly
                # created handler before it can enter its try/finally block.
                await self.broker.revoke(name, 1)
                self.assertEqual({}, self.broker._leases)
            finally:
                grant.close_worker_endpoint()
        state = self.broker.status()
        self.assertEqual(300, len(state["grants"]))
        self.assertTrue(all(item["status"] == "REVOKED" for item in state["grants"]))
        self.assertEqual(0, state["global_in_flight"])
        original = self.admission["rolling-0"]["binding"]
        with self.assertRaises(Fault) as caught:
            await self.broker.grant(original, "fixture")
        self.assertEqual("BROKER_GRANT_REUSED", caught.exception.code)

    async def test_live_grants_remain_bounded_after_retirement_fix(self):
        grants = []
        try:
            for index in range(256):
                name = f"live-{index}"
                binding = {"attempt_id": name, "generation": 1, "launch_key": f"launch:{name}:1"}
                self.admission[name] = {"binding": binding, "phase": "starting"}
                grants.append(await self.broker.grant(binding, "fixture"))
            binding = {"attempt_id": "over-limit", "generation": 1, "launch_key": "launch:over-limit:1"}
            self.admission["over-limit"] = {"binding": binding, "phase": "starting"}
            with self.assertRaises(Fault) as caught:
                await self.broker.grant(binding, "fixture")
            self.assertEqual("BROKER_STATE_ERROR", caught.exception.code)
            await self.broker.revoke("live-0", 1)
            grants.append(await self.broker.grant(binding, "fixture"))
            self.assertEqual(256, len(self.broker._leases))
        finally:
            for grant in grants:
                grant.close_worker_endpoint()
                await self.broker.revoke(grant.binding["attempt_id"], grant.binding["generation"])

    async def test_recovery_revokes_crash_left_grants_without_refunding_requests(self):
        client = await self.client()
        await self.request(client)
        await self.broker.close()
        # A synthetic crash snapshot leaves an ACTIVE grant and IN_FLIGHT call.
        # Startup must treat the call as uncertain, never replay or refund it.
        db = sqlite3.connect(self.root / "broker/journal.sqlite3")
        db.execute("UPDATE grants SET status='ACTIVE',reason=NULL")
        db.execute("UPDATE requests SET state='IN_FLIGHT',outcome=NULL,finished_at=NULL")
        db.commit()
        db.close()
        restarted = await self.make_broker(directory="broker")
        state = restarted.status()
        self.assertEqual("REVOKED", state["grants"][0]["status"])
        self.assertEqual("owner_restarted", state["grants"][0]["reason"])
        self.assertEqual(1, state["grants"][0]["requests_consumed"])
        self.assertEqual("UNCERTAIN", state["requests"][0]["state"])
        self.assertEqual("owner_restarted", state["requests"][0]["outcome"])

    async def test_controller_process_death_closes_channel_and_recovery_is_uncertain(self):
        control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        control.settimeout(3)
        source = Path(__file__).resolve().parents[1] / "src"
        child_code = r'''
import array, asyncio, json, socket, sys
sys.path.insert(0, sys.argv[1])
from xiaolvji.provider_broker import ProviderBroker, ProviderProfile
async def main():
    profile = ProviderProfile("fixture", "openai-responses", "https://fixture.invalid/registered/responses",
                              "fixture-model", {"reasoning": {"effort": "high"}}, request_timeout=30)
    broker = ProviderBroker(sys.argv[2], {"fixture": profile},
                            {"fixture": "synthetic-upstream-key-DO-NOT-REVEAL"}, lambda binding, phase: True)
    async def fixture(profile):
        return await asyncio.open_connection("127.0.0.1", int(sys.argv[4]), family=socket.AF_INET)
    broker._connect_upstream = fixture
    await broker.start()
    grant = await broker.grant({"attempt_id": "killed", "generation": 1, "launch_key": "launch:killed:1"}, "fixture")
    channel = socket.socket(fileno=int(sys.argv[3]))
    channel.sendmsg([json.dumps({"capability": grant.capability}).encode()],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [grant.worker_socket.fileno()]))])
    grant.close_worker_endpoint()
    channel.close()
    await asyncio.Event().wait()
asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", child_code, str(source), str(self.root / "process-crash"),
            str(child_control.fileno()), str(self.port), pass_fds=(child_control.fileno(),),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        child_control.close()
        writer = None
        try:
            raw, ancillary, flags, address = await asyncio.to_thread(control.recvmsg, 1024, socket.CMSG_SPACE(array.array("i").itemsize))
            self.assertEqual(0, flags & socket.MSG_CTRUNC)
            descriptors = array.array("i")
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
            self.assertEqual(1, len(descriptors))
            endpoint = socket.socket(fileno=descriptors[0])
            reader, writer = await asyncio.open_connection(sock=endpoint)
            token = json.loads(raw)["capability"]
            self.response_mode = "hold"
            await write_frame(writer, {"v": 1, "request_id": "crash-request", "capability": token,
                                      "method": "POST", "path": "/v1/responses",
                                      "body": {"model": "fixture-model", "input": "synthetic", "reasoning": {"effort": "high"}}})
            await asyncio.wait_for(self.upstream_entered.wait(), 3)
            self.assertEqual("response", (await asyncio.wait_for(read_frame(reader), 3))["type"])
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
            self.assertEqual(b"", await asyncio.wait_for(reader.read(), 3))
            await asyncio.wait_for(self.upstream_closed.wait(), 3)
            restarted = await self.make_broker(directory="process-crash")
            state = restarted.status()
            self.assertEqual("REVOKED", state["grants"][0]["status"])
            self.assertEqual("owner_restarted", state["grants"][0]["reason"])
            self.assertEqual(1, state["grants"][0]["requests_consumed"])
            self.assertEqual("UNCERTAIN", state["requests"][0]["state"])
        finally:
            control.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if process.returncode is None:
                process.kill()
                await process.wait()
            if process.stderr is not None:
                diagnostic = await process.stderr.read()
                self.assertEqual(b"", diagnostic)

    async def test_accounting_failure_closes_all_channels_before_outbound_traffic(self):
        first, second = await self.client("first"), await self.client("second")
        def deny_grant_update(action, table, column, db_name, trigger):
            if action == sqlite3.SQLITE_UPDATE and table == "grants":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.broker._db.set_authorizer(deny_grant_update)
        await write_frame(first[1], self.request_frame(first))
        self.assertEqual(b"", await asyncio.wait_for(first[0].read(), 1))
        self.assertEqual(b"", await asyncio.wait_for(second[0].read(), 1))
        self.assertEqual([], self.received)
        self.assertFalse(self.broker.status()["journal_healthy"])
        self.broker._db.set_authorizer(None)
        with self.assertRaises(Fault) as caught:
            await self.client("third")
        self.assertEqual("BROKER_STATE_ERROR", caught.exception.code)

    async def test_revocation_interrupts_actual_downstream_backpressure(self):
        binding = {"attempt_id": "backpressure", "generation": 1, "launch_key": "launch:backpressure:1"}
        self.admission["backpressure"] = {"binding": binding, "phase": "starting"}
        grant = await self.broker.grant(binding, "fixture")
        self.admission["backpressure"]["phase"] = "running"
        endpoint = grant.worker_socket
        endpoint.setblocking(False)
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        lease = self.broker._leases[("backpressure", 1)]
        lease.writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        lease.writer.transport.set_write_buffer_limits(high=1024, low=512)
        self.response = b"x" * (512 * 1024)
        frame = self.request_frame((None, None, grant))
        data = canonical_json(frame).encode()
        try:
            await asyncio.get_running_loop().sock_sendall(endpoint, struct.pack("!I", len(data)) + data)
            async with asyncio.timeout(1):
                while lease.writer.transport.get_write_buffer_size() == 0:
                    await asyncio.sleep(0.005)
            self.assertEqual(1, self.broker.status()["global_in_flight"])
            await asyncio.wait_for(self.broker.revoke("backpressure", 1), 1)
            self.assertEqual(0, self.broker.status()["global_in_flight"])
            self.assertEqual("LOCAL_ENDED", self.broker.status()["requests"][0]["state"])
            self.assertGreater(self.broker.status()["requests"][0]["response_bytes"], 0)
        finally:
            grant.close_worker_endpoint()

    async def test_admission_callback_exception_is_closed_and_redacted(self):
        def failed(_binding, _phase):
            raise RuntimeError(SECRET)
        self.broker._admission = failed
        with self.assertRaises(Fault) as caught:
            await self.client()
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        self.assertNotIn(SECRET, str(caught.exception))

    async def test_socketpair_unavailable_never_creates_a_usable_grant(self):
        with patch("xiaolvji.provider_broker.socket.socketpair", side_effect=PermissionError("fixture")):
            with self.assertRaises(Fault) as caught:
                await self.client()
        self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)
        self.assertEqual([], self.broker.status()["grants"])

    async def test_missing_posix_locking_is_structured_and_profile_validation_stays_available(self):
        candidate = ProviderBroker(self.root / "unsupported-platform", {"fixture": profile()},
                                   {"fixture": SECRET}, self.check_admission)
        real_import = builtins.__import__
        def unavailable(name, *args, **kwargs):
            if name == "fcntl":
                raise ModuleNotFoundError("synthetic unavailable POSIX locking")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable):
            self.assertEqual(profile().to_dict(), normalize_provider_profile(profile().to_dict()).to_dict())
            with self.assertRaises(Fault) as caught:
                await candidate.start()
        self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)
        self.assertFalse((self.root / "unsupported-platform").exists())

    async def test_state_directory_permissions_and_symlink_aliases_are_rejected(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o755)
        candidate = ProviderBroker(unsafe, {"fixture": profile()}, {"fixture": SECRET}, self.check_admission)
        with self.assertRaises(Fault) as caught:
            await candidate.start()
        self.assertEqual("UNSAFE_STATE", caught.exception.code)
        alias = self.root / "alias"
        alias.symlink_to(self.root / "broker", target_is_directory=True)
        with self.assertRaises(Fault):
            ProviderBroker(alias, {"fixture": profile()}, {"fixture": SECRET}, self.check_admission)

    async def test_production_connection_requires_default_verified_tls(self):
        chosen = profile()
        # The real connector is inspected at its networking call, never allowed
        # to contact an external provider in this test suite.
        with patch("xiaolvji.provider_broker.asyncio.open_connection") as opened:
            opened.return_value = (None, None)
            await ProviderBroker._connect_upstream(self.broker, chosen)
        kwargs = opened.call_args.kwargs
        import ssl
        self.assertEqual(ssl.CERT_REQUIRED, kwargs["ssl"].verify_mode)
        self.assertTrue(kwargs["ssl"].check_hostname)
        self.assertEqual("fixture.invalid", kwargs["server_hostname"])
        self.assertEqual(("fixture.invalid", 443), opened.call_args.args)


if __name__ == "__main__":
    unittest.main()
