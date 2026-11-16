"""Explicit synthetic provider fixture for the reversible local demo only.

This server and credential are generated locally. Production ProviderBroker
always uses the registered HTTPS endpoint and never enables this connector
through configuration, environment variables, or a worker request.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys

from .provider_bridge import BRIDGE_PORT
from .provider_broker import ProviderBroker
from .provider_config import normalize_provider_configuration
from .provider_runtime import ProviderRuntime


class DemoProvider:
    def __init__(self, state, ledger):
        self.state = Path(state)
        self.ledger = ledger
        self.server = None
        self.modules = {}
        self.requests = []
        self._connections = set()
        self._handlers = set()
        self.in_flight = self.maximum_in_flight = 0
        self.credential = "xiaolvji-local-demo-synthetic-credential-only"

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=32768)
        port = self.server.sockets[0].getsockname()[1]
        class FixtureBroker(ProviderBroker):
            async def _connect_upstream(self, profile):
                return await asyncio.open_connection("127.0.0.1", port, limit=32768)
        host = {"schema_version": 1, "engine": "codex", "provider_id": "local-fixture",
                "api": "openai-responses", "model": "xiaolvji-demo-fixture",
                "quality": {"model_reasoning_effort": "high"}}
        profile = {"profile_id": "local-fixture", "api": "openai-responses",
                   "upstream_url": "https://fixture.invalid/v1/responses", "model": host["model"],
                   "request_constraints": {"reasoning": {"effort": "high"}},
                   "max_requests": 2, "max_concurrency": 2, "request_timeout": 10}
        configuration = normalize_provider_configuration({"schema_version": 1, "providers": [profile],
            "bindings": [{"project_id": "demo", "host": host}], "global_request_limit": 2,
            "credential_files": {"local-fixture": str(self.state / "unused-demo-credential")}})
        self.runtime = ProviderRuntime(self.state / "providers", self.ledger, configuration,
                                      broker_factory=FixtureBroker, credentials={"local-fixture": self.credential})
        return self.runtime

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        self._connections.add(writer)
        counted = False
        try:
            async with asyncio.timeout(12):
                header = await reader.readuntil(b"\r\n\r\n")
                if len(header) > 32768:
                    return
                lines = header.decode("ascii").split("\r\n")
                headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
                if lines[0] != "POST /v1/responses HTTP/1.1" or headers.get("authorization") != "Bearer " + self.credential:
                    return
                length = int(headers["content-length"])
                if not 0 < length <= 8192:
                    return
                body = json.loads(await reader.readexactly(length))
                name = body["input"]
                if name not in self.modules or body.get("model") != "xiaolvji-demo-fixture" or body.get("reasoning") != {"effort": "high"}:
                    return
                self.requests.append({"module": name, "model": body["model"], "quality": body["reasoning"]})
                self.in_flight += 1
                counted = True
                self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
                await asyncio.sleep(.03)
                data = json.dumps({"synthetic_fixture": True, "content": self.modules[name]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                             str(len(data)).encode() + b"\r\n\r\n" + data)
                await writer.drain()
        except (OSError, ValueError, KeyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            if counted:
                self.in_flight -= 1
            writer.close()
            self._connections.discard(writer)
            self._handlers.discard(task)

    def command(self, name, content, delay):
        self.modules[name] = content
        script = (
            "import http.client,json,os,pathlib,time; "
            "body=json.dumps({'model':'xiaolvji-demo-fixture','input':" + repr(name) +
            ",'reasoning':{'effort':'high'},'store':False}); "
            "client=http.client.HTTPConnection('127.0.0.1'," + str(BRIDGE_PORT) + ",timeout=12); "
            "client.request('POST','/v1/responses',body,{'Content-Type':'application/json',"
            "'Authorization':'Bearer '+os.environ['XIAOLVJI_PROVIDER_CAPABILITY']}); "
            "response=client.getresponse(); assert response.status==200; result=json.loads(response.read()); client.close(); "
            "assert result['synthetic_fixture'] is True; time.sleep(" + repr(delay) + "); "
            "pathlib.Path(" + repr("src/" + name + ".py") + ").write_text(result['content'])"
        )
        return {"argv": [sys.executable, "-B", "-c", script], "stdin_text": None, "env": {}}

    async def close(self):
        if hasattr(self, "runtime"):
            await self.runtime.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self._connections):
            writer.close()
        pending = list(self._handlers)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def report(self):
        path = self.state / "providers/journal.sqlite3"
        if not path.exists():
            grants, requests = [], []
        else:
            connection = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                grants = [dict(row) for row in connection.execute("SELECT attempt_id,status,requests_consumed FROM grants")]
                requests = [dict(row) for row in connection.execute("SELECT state,outcome FROM requests")]
            finally:
                connection.close()
        return {"synthetic_only": True, "real_provider_requests": 0, "real_host_invocations": 0,
                "http_requests": len(self.requests), "maximum_local_provider_in_flight": self.maximum_in_flight,
                "grants": grants, "requests": requests,
                "all_grants_revoked": len(grants) == 4 and all(row["status"] == "REVOKED" for row in grants),
                "all_requests_accounted": len(requests) == 4 and all(row["state"] == "COMPLETED" and row["outcome"] == "completed" for row in requests),
                "remote_processing_after_disconnect": "unknown_for_real_providers"}
