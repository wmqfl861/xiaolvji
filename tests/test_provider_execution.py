"""Real namespace/bridge/broker execution with a synthetic local upstream only.

The network fixture replaces the broker's private connection seam; registered
production profiles still require HTTPS. No model binary, provider account or
real authentication participates in these integration tests.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Principal, digest
from xiaolvji.dispatcher import Dispatcher
from xiaolvji.executor import ProcessExecutor
from xiaolvji.host_config import render_host_config
from xiaolvji.integration import integration_baseline
from xiaolvji.pipeline import Pipeline
from xiaolvji.provider_bridge import BRIDGE_URL
from xiaolvji.provider_broker import ProviderBroker, ProviderProfile
from xiaolvji.provider_config import normalize_provider_configuration
from xiaolvji.provider_runtime import ProviderRuntime
from xiaolvji.sandbox import LinuxSandbox
from xiaolvji.store import Ledger
from xiaolvji.supervisor import SandboxSetupError, _validate_attestation
from xiaolvji.workspace import git


SECRET = "synthetic-integration-upstream-key-NEVER-WORKER"
SNAPSHOT = {"schema_version": 1, "engine": "codex", "provider_id": "fixture",
            "api": "openai-responses", "model": "fixture-model",
            "quality": {"model_reasoning_effort": "high"}}


class LocalFixtureBroker(ProviderBroker):
    """Only this test subclass can connect to the synthetic plaintext fixture."""

    async def _connect_upstream(self, profile):
        return await asyncio.open_connection("127.0.0.1", self.fixture_port, family=socket.AF_INET)

    async def close(self):
        if self._db is not None:
            self.before_close = self.status()
        await super().close()


class ProviderExecutionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            capability = LinuxSandbox(Path(directory) / "state").probe()
        if not capability["supported"]:
            raise unittest.SkipTest("Real Linux sandbox unavailable: " + str(capability))

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "controller"
        self.state.mkdir()
        self.secret_file = self.state / "synthetic-credential"
        self.secret_file.write_text(SECRET)
        self.executor = ProcessExecutor(self.state, profile="controlled_executor")
        self.admission = {}
        self.requests = []
        self.grants = []
        self.upstream_tasks = set()
        self.upstream_writers = set()
        self.upstream_entered = asyncio.Event()
        self.upstream_closed = asyncio.Event()
        self.stream_release = asyncio.Event()
        self.stream_release.set()
        self.hold_response = False
        self.server = await asyncio.start_server(self.upstream, "127.0.0.1", 0, family=socket.AF_INET)
        self.profile = ProviderProfile("fixture", "openai-responses",
                                       "https://fixture.invalid/registered/responses", "fixture-model",
                                       {"reasoning": {"effort": "high"}}, request_timeout=10)
        self.broker = LocalFixtureBroker(self.state / "broker", {"fixture": self.profile},
                                         {"fixture": SECRET}, self.check_admission, 2)
        self.broker.fixture_port = self.server.sockets[0].getsockname()[1]
        await self.broker.start()

    async def asyncTearDown(self):
        self.stream_release.set()
        for grant in self.grants:
            grant.close_worker_endpoint()
        await self.broker.close()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.upstream_writers):
            writer.close()
        for task in list(self.upstream_tasks):
            task.cancel()
        if self.upstream_tasks:
            await asyncio.gather(*self.upstream_tasks, return_exceptions=True)
        self.temporary.cleanup()

    def check_admission(self, binding, phase):
        expected = self.admission.get(binding["attempt_id"])
        return (expected is not None and expected["binding"] == binding
                and expected["phase"] == ("starting" if phase == "grant" else "running"))

    async def upstream(self, reader, writer):
        task = asyncio.current_task()
        self.upstream_tasks.add(task)
        self.upstream_writers.add(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode("ascii").split("\r\n")
            headers = {name.lower(): value.strip() for name, value in
                       (line.split(":", 1) for line in lines[1:] if line)}
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            self.requests.append({"start": lines[0], "headers": headers, "body": body})
            self.upstream_entered.set()
            if self.hold_response:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                await reader.read()
                self.upstream_closed.set()
                return
            if body["stream"]:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                first = b'event: response.output_text.delta\ndata: {"delta":"' + b"x" * 120 + b'"}\n\n'
                writer.write(f"{len(first):x}\r\n".encode() + first + b"\r\n")
                await writer.drain()
                await self.stream_release.wait()
                final = b'data: {"secret_echo":"' + SECRET.encode() + b'"}\n\ndata: [DONE]\n\n'
                # The secret crosses two upstream chunks, exercising the
                # broker's stream redaction before the bridge sees any bytes.
                for chunk in (final[:31], final[31:]):
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                writer.write(b"0\r\n\r\n")
            else:
                payload = json.dumps({"output": "synthetic success", "secret_echo": SECRET}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(payload)).encode() + b"\r\nX-Provider-Secret: " + SECRET.encode()
                             + b"\r\n\r\n" + payload)
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

    async def new_grant(self, name):
        binding = {"attempt_id": name, "generation": 1, "launch_key": "launch:" + name}
        self.admission[name] = {"binding": binding, "phase": "starting"}
        grant = await self.broker.grant(binding, "fixture", ttl_seconds=30)
        self.grants.append(grant)
        return grant

    async def execute(self, code, name="one", *, timeout=10, on_started=None, grant=None):
        grant = grant or await self.new_grant(name)
        rendered = render_host_config(SNAPSHOT, BRIDGE_URL, grant.capability)
        runtime = {"version": 1, "capability": grant.capability, "api": self.profile.api,
                   "env": rendered["env"], "max_request_bytes": self.profile.max_request_bytes,
                   "request_timeout": self.profile.request_timeout}
        policy = {"workspace": str(self.work), "provider_transport": {"fd": grant.worker_socket.fileno()},
                  "home_files": [{"path": item["relative_path"], "content": item["content"]}
                                 for item in rendered["files"]]}
        async def authorized(identity):
            if on_started is not None:
                await on_started(identity)
            self.admission[name]["phase"] = "running"
            # The outside supervisor has inherited its own endpoint already.
            grant.close_worker_endpoint()
        try:
            return await self.executor.run(grant.binding, [sys.executable, "-I", "-S", "-c", code],
                                           str(self.work), env={"OPENAI_API_KEY": SECRET}, timeout=timeout,
                                           on_started=authorized, sandbox_policy=policy, provider_runtime=runtime)
        finally:
            grant.close_worker_endpoint()
            await self.broker.revoke(name, 1)

    async def await_file(self, name, timeout=6):
        async with asyncio.timeout(timeout):
            while not (self.work / name).exists():
                await asyncio.sleep(.01)
        return (self.work / name).read_text()

    def client_prelude(self):
        return """
import http.client,json,os,pathlib,sys,tomllib,urllib.parse
home=pathlib.Path(os.environ['HOME']); config=home/'.codex/config.toml'
settings=tomllib.loads(config.read_text()); provider=settings['model_providers'][settings['model_provider']]
base=urllib.parse.urlsplit(provider['base_url']); token=os.environ[provider['env_key']]
def request(stream):
 conn=http.client.HTTPConnection(base.hostname,base.port,timeout=12)
 body=json.dumps({'model':settings['model'],'reasoning':{'effort':settings['model_reasoning_effort']},
                  'input':'synthetic fixture','store':False,'stream':stream})
 conn.request('POST',base.path+'/responses',body,{'Authorization':'Bearer '+token,'Content-Type':'application/json'})
 return conn,conn.getresponse()
"""

    def assert_revoked(self, name):
        status = self.broker.status()
        self.assertEqual(0, status["global_in_flight"])
        matching = [grant for grant in status["grants"] if grant["attempt_id"] == name]
        self.assertEqual(1, len(matching))
        self.assertEqual("REVOKED", matching[0]["status"])

    def pipeline_fixture(self, names, *, auto_integrate=True):
        source = self.root / "source"
        source.mkdir()
        (source / "src").mkdir()
        (source / "src/baseline.txt").write_text("baseline\n")
        git("init", cwd=source)
        git("add", ".", cwd=source)
        git("commit", "-m", "synthetic provider pipeline baseline", cwd=source)
        baseline = git("rev-parse", "HEAD", cwd=source)
        root = self.root / "pipeline-controller"
        operator = Principal("provider-fixture-operator", "operator")
        planner = Principal("provider-fixture-planner", "planner")
        reviewer = Principal("provider-fixture-reviewer", "reviewer")
        ledger = Ledger(root / "ledger.sqlite3", global_limit=2)
        ledger.enable_project("fixture", source, {"project_limit": 2, "profile": "controlled_executor",
                                                   "review_authority": reviewer.id}, operator, "enable")
        scopes = [{"path": "src", "kind": "directory"}]
        plan = {"id": "provider-pipeline-plan", "revision": 1, "scope_digest": digest(scopes),
                "approved_scope": scopes, "approval_ref": "fixture:explicit-test-approval", "contracts": []}
        ledger.register_plan("fixture", plan, planner, "plan")
        for name in names:
            check = """
import errno,json,os,pathlib,socket
assert pathlib.Path(RESULT).read_text()==EXPECTED
assert not pathlib.Path(os.environ['HOME'],'.codex/config.toml').exists()
assert 'XIAOLVJI_PROVIDER_CAPABILITY' not in os.environ
assert bool(os.statvfs('.').f_flag & os.ST_RDONLY)
try: socket.socket(socket.AF_INET,socket.SOCK_STREAM)
except OSError as exc: assert exc.errno==errno.EPERM
else: raise AssertionError('quality command unexpectedly received network authority')
print(json.dumps({'offline_check':True}))
""".replace("RESULT", repr("src/" + name + "/result.txt")).replace("EXPECTED", repr(name))
            task = {"schema_version": 1, "task_id": name,
                    "plan": {key: plan[key] for key in ("id", "revision", "scope_digest")},
                    "role": "worker", "engine": "codex", "objective": "Produce synthetic result " + name,
                    "base_commit": baseline, "contract_versions": [], "depends_on": [],
                    "write_scope": [{"path": "src/" + name, "kind": "directory"}],
                    "exclusive_resources": [], "acceptance": [{"id": "offline-behavior", "required": True,
                        "criterion": "Exact output passes without provider or write authority",
                        "argv": [sys.executable, "-I", "-S", "-c", check], "cwd": ".", "timeout_ms": 5000}],
                    "expected_artifacts": ["candidate_commit", "validation_report"],
                    "retry": {"max_retries": 0, "backoff_ms": 0}, "final_review_required": True}
            ledger.register_task("fixture", task, planner, "task:" + name)
        configuration = normalize_provider_configuration({"schema_version": 1,
            "providers": [self.profile.to_dict()], "bindings": [{"project_id": "fixture", "host": SNAPSHOT}],
            "credential_files": {"fixture": str(self.secret_file)}, "global_request_limit": 2})
        brokers = []
        def factory(*args, **kwargs):
            broker = LocalFixtureBroker(*args, **kwargs)
            broker.fixture_port = self.server.sockets[0].getsockname()[1]
            brokers.append(broker)
            return broker
        runtime = ProviderRuntime(root / "provider", ledger, configuration,
                                  broker_factory=factory, credentials={"fixture": SECRET})
        workspaces = {}
        def command(attempt, workspace, prompt):
            name = attempt["task_id"]
            workspaces[name] = Path(workspace["path"])
            code = self.client_prelude() + """
name=TASK
directory=pathlib.Path('src')/name
conn,response=request(name=='slow')
assert response.status==200
if name=='slow':
 first=response.readline().decode();(directory/'stream-started').write_text(first)
body=response.read().decode();conn.close()
(directory/'result.txt').write_text(name)
print(json.dumps({'task':name,'response_received':bool(body)}))
""".replace("TASK", repr(name))
            return {"argv": [sys.executable, "-I", "-S", "-c", code], "stdin_text": None, "env": {}}
        pipeline = Pipeline(root / "pipeline", ledger, command_factory=command,
                            provider_runtime=runtime, auto_integrate=auto_integrate)
        executor = ProcessExecutor(root / "executor", profile="controlled_executor")
        events = []
        dispatcher = Dispatcher(ledger, executor, pipeline, events.append)
        return {"root": root, "source": source, "baseline": baseline, "ledger": ledger, "runtime": runtime,
                "pipeline": pipeline, "dispatcher": dispatcher, "events": events,
                "workspaces": workspaces, "brokers": brokers}

    async def await_pipeline_file(self, fixture, task, relative):
        async with asyncio.timeout(12):
            while (task not in fixture["workspaces"] or
                   not (fixture["workspaces"][task] / relative).exists()):
                await asyncio.sleep(.01)
        return fixture["workspaces"][task] / relative

    async def test_json_and_live_sse_preserve_the_strict_boundary(self):
        self.stream_release.clear()
        code = self.client_prelude() + """
pathlib.Path('started').write_text('authorized')
fds={}
for fd in os.listdir('/proc/self/fd'):
 try: fds[fd]=os.readlink('/proc/self/fd/'+fd)
 except FileNotFoundError: pass
try: config.write_text('replace'); readonly=False
except OSError: readonly=True
try: config.parent.rename(home/'moved'); ancestor_fixed=False
except OSError: ancestor_fixed=True
conn,response=request(False); plain=json.loads(response.read()); plain_status=response.status
headers=dict(response.getheaders());conn.close()
conn,response=request(True); first=response.readline().decode();pathlib.Path('stream-first').write_text(first)
stream=first+response.read().decode();stream_status=response.status;conn.close()
print(json.dumps({'plain':plain,'stream':stream,'statuses':[plain_status,stream_status],
 'headers':headers,'readonly':readonly,'ancestor_fixed':ancestor_fixed,'fds':fds,
 'credential_env_present':'OPENAI_API_KEY' in os.environ,'controller_visible':pathlib.Path(SECRET_FILE).exists()}))
""".replace("SECRET_FILE", repr(str(self.secret_file)))
        async def started(identity):
            directory = self.state / "launches/one"
            self.assertTrue((directory / "identity.json").exists())
            for _ in range(2):
                self.assertFalse((directory / "worker.json").exists())
                self.assertFalse((self.work / "started").exists())
                self.assertEqual([], self.requests)
                await asyncio.sleep(.025)
        task = asyncio.create_task(self.execute(code, on_started=started))
        try:
            first = await self.await_file("stream-first")
            self.assertEqual("event: response.output_text.delta\n", first)
            self.assertEqual(2, len(self.requests))
            self.assertFalse(task.done(), "SSE must arrive before the upstream response finishes")
        finally:
            self.stream_release.set()
            result = await asyncio.wait_for(task, 12)
        self.assertEqual(0, result["exit_code"], Path(result["stderr_path"]).read_text())
        observed = json.loads(Path(result["stdout_path"]).read_text())
        self.assertEqual([200, 200], observed["statuses"])
        self.assertEqual("synthetic success", observed["plain"]["output"])
        self.assertEqual("[REDACTED]", observed["plain"]["secret_echo"])
        self.assertIn("data: [DONE]", observed["stream"])
        self.assertNotIn(SECRET, json.dumps(observed))
        self.assertFalse(observed["credential_env_present"])
        self.assertFalse(observed["controller_visible"])
        self.assertTrue(observed["readonly"])
        self.assertTrue(observed["ancestor_fixed"])
        self.assertEqual({"0", "1", "2"}, set(observed["fds"]))
        for request in self.requests:
            self.assertEqual("POST /registered/responses HTTP/1.1", request["start"])
            self.assertEqual("Bearer " + SECRET, request["headers"]["authorization"])
            self.assertEqual("fixture-model", request["body"]["model"])
            self.assertEqual({"effort": "high"}, request["body"]["reasoning"])
        attestation = result["sandbox"]
        self.assertTrue(attestation["attested"])
        self.assertEqual("brokered-provider-v1", attestation["policy"]["network"])
        self.assertEqual({"interfaces": ["lo"], "loopback_up": True, "external_ipv4_routes": 0},
                         attestation["actual"]["provider_network"])
        self.assertEqual(0, attestation["actual"]["provider_transport"]["peer_pid"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertLessEqual((self.state / "launches/one/sandbox.json").stat().st_mtime_ns,
                             (self.work / "started").stat().st_mtime_ns)
        for file in self.state.rglob("*"):
            if file.is_file() and file != self.secret_file:
                self.assertNotIn(SECRET.encode(), file.read_bytes(), file.name)
        self.assert_revoked("one")

        # A real completed launch supplies all other report fields here. The
        # channel mismatch must be rejected before any process-identity lookup.
        report = {**attestation["actual"], "protocol": "xiaolvji-sandbox-bootstrap-v1", "nonce": "fixture",
                  "policy_digest": attestation["policy_digest"]}
        report["provider_transport"] = dict(report["provider_transport"], inode=-1)
        request = {"nonce": "fixture", "readonly_mounts": attestation["policy"]["readonly_mounts"],
                   "writable_mounts": attestation["policy"]["writable_mounts"]}
        parent_namespaces = {name: os.readlink("/proc/self/ns/" + name) for name in report["namespaces"]}
        with self.assertRaisesRegex(SandboxSetupError, "sandbox_provider_channel_mismatch"):
            _validate_attestation(report, request, parent_namespaces, str(self.work), attestation,
                                  attestation["launcher_identity"])

    async def test_authorization_failure_never_starts_client_or_upstream(self):
        async def rejected(identity):
            self.assertEqual([], self.requests)
            raise RuntimeError("synthetic ledger rejection")
        with self.assertRaisesRegex(RuntimeError, "synthetic ledger rejection"):
            await self.execute("from pathlib import Path; Path('forbidden').touch()", on_started=rejected)
        self.assertFalse((self.work / "forbidden").exists())
        self.assertFalse((self.state / "launches/one/worker.json").exists())
        self.assertFalse((self.state / "launches/one/sandbox.json").exists())
        self.assertEqual([], self.requests)
        identity = json.loads((self.state / "launches/one/identity.json").read_text())
        result = self.executor.reconcile(identity)
        self.assertTrue(result["no_process_created"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertFalse(result["worker_started"])
        self.assert_revoked("one")

    async def test_timeout_revokes_stream_and_confirms_namespace_exit(self):
        self.hold_response = True
        code = self.client_prelude() + "conn,response=request(True); response.read(); pathlib.Path('late').touch()"
        task = asyncio.create_task(self.execute(code, name="timeout", timeout=.8))
        try:
            await asyncio.wait_for(self.upstream_entered.wait(), 6)
            result = await asyncio.wait_for(task, 6)
        finally:
            if not task.done():
                await self.executor.cancel("timeout", grace=.05)
                await task
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        await asyncio.wait_for(self.upstream_closed.wait(), 3)
        self.assertFalse((self.work / "late").exists())
        self.assert_revoked("timeout")

    async def test_cancel_revokes_stream_and_confirms_namespace_exit(self):
        self.hold_response = True
        code = self.client_prelude() + "conn,response=request(True); response.read(); pathlib.Path('late').touch()"
        task = asyncio.create_task(self.execute(code, name="cancel"))
        try:
            await asyncio.wait_for(self.upstream_entered.wait(), 6)
            cancellation = await self.executor.cancel("cancel", grace=.05)
            result = await asyncio.wait_for(task, 6)
        finally:
            if not task.done():
                await self.executor.cancel("cancel", grace=.05)
                await task
        self.assertTrue(cancellation["process_tree_terminated"])
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        await asyncio.wait_for(self.upstream_closed.wait(), 3)
        self.assertFalse((self.work / "late").exists())
        self.assert_revoked("cancel")

    async def test_client_disconnect_revokes_upstream_while_worker_is_still_live(self):
        self.hold_response = True
        code = self.client_prelude() + """
import time
conn,response=request(True);response.close();conn.close()
pathlib.Path('client-closed').touch()
while not pathlib.Path('disconnect-observed').exists(): time.sleep(.01)
print(json.dumps({'worker_survived_client_disconnect':True}))
"""
        task = asyncio.create_task(self.execute(code, name="disconnect"))
        try:
            await self.await_file("client-closed")
            await asyncio.wait_for(self.upstream_closed.wait(), 3)
            self.assertFalse(task.done())
            self.assert_revoked("disconnect")
            (self.work / "disconnect-observed").touch()
            result = await asyncio.wait_for(task, 6)
        finally:
            if not task.done():
                await self.executor.cancel("disconnect", grace=.05)
                await task
        self.assertEqual(0, result["exit_code"], Path(result["stderr_path"]).read_text())
        self.assertEqual({"worker_survived_client_disconnect": True},
                         json.loads(Path(result["stdout_path"]).read_text()))
        self.assertTrue(result["process_tree_terminated"])

    async def test_bridge_and_broker_reject_authority_and_quality_changes_before_upstream(self):
        code = self.client_prelude() + """
statuses=[]
for bad in ('authorization','path','model','quality'):
 conn=http.client.HTTPConnection(base.hostname,base.port,timeout=5)
 body={'model':settings['model'],'reasoning':{'effort':settings['model_reasoning_effort']},
       'input':'synthetic fixture','store':False,'stream':False}
 if bad=='model': body['model']='weaker-model'
 if bad=='quality': body['reasoning']['effort']='low'
 path='/unregistered-route' if bad=='path' else base.path+'/responses'
 capability='invalid-capability' if bad=='authorization' else token
 conn.request('POST',path,json.dumps(body),{'Authorization':'Bearer '+capability,'Content-Type':'application/json'})
 response=conn.getresponse();statuses.append(response.status);response.read();conn.close()
conn,response=request(False);payload=json.loads(response.read());statuses.append(response.status);conn.close()
print(json.dumps({'statuses':statuses,'output':payload['output']}))
"""
        result = await self.execute(code, name="rejected")
        self.assertEqual(0, result["exit_code"], Path(result["stderr_path"]).read_text())
        observed = json.loads(Path(result["stdout_path"]).read_text())
        self.assertEqual([403, 404, 502, 502, 200], observed["statuses"])
        self.assertEqual("synthetic success", observed["output"])
        self.assertEqual(1, len(self.requests))
        self.assertEqual("fixture-model", self.requests[0]["body"]["model"])
        status = self.broker.status()
        self.assertEqual(1, status["grants"][0]["requests_consumed"])
        self.assert_revoked("rejected")

    async def test_channel_substitution_after_preflight_fails_before_worker(self):
        grant = await self.new_grant("changed")
        other, other_peer = socket.socketpair()
        with other, other_peer:
            real_preflight = self.executor._sandbox_preflight
            def substituted(*args):
                result = real_preflight(*args)
                os.dup2(other.fileno(), grant.worker_socket.fileno(), inheritable=False)
                return result
            with patch.object(self.executor, "_sandbox_preflight", side_effect=substituted):
                result = await self.execute("from pathlib import Path; Path('forbidden').touch()",
                                            name="changed", grant=grant)
        self.assertNotEqual(0, result["exit_code"])
        self.assertTrue(result["no_process_created"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertFalse(result["worker_started"])
        self.assertFalse((self.work / "forbidden").exists())
        self.assertFalse((self.state / "launches/changed/worker.json").exists())
        self.assertFalse((self.state / "launches/changed/sandbox.json").exists())
        self.assertEqual([], self.requests)
        self.assert_revoked("changed")

    async def test_pipeline_refills_slots_and_keeps_quality_checks_offline(self):
        self.stream_release.clear()
        fixture = self.pipeline_fixture(["slow", "fast", "next"])
        run = asyncio.create_task(fixture["dispatcher"].run("fixture", timeout=30))
        try:
            await self.await_pipeline_file(fixture, "slow", "src/slow/stream-started")
            await self.await_pipeline_file(fixture, "next", "src/next/result.txt")
            self.assertFalse((fixture["workspaces"]["slow"] / "src/slow/result.txt").exists())
            self.assertFalse(run.done(), "A freed slot must refill while its unrelated sibling is still streaming")
            self.stream_release.set()
            result = await asyncio.wait_for(run, 25)
        finally:
            self.stream_release.set()
            if not run.done():
                await fixture["dispatcher"].cancel("fixture")
                await run
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["status"]["capacity"]["used"])
        capacities = [event["used"] for event in fixture["events"] if event["type"] == "capacity"]
        self.assertTrue(capacities)
        self.assertEqual(2, max(capacities))
        self.assertTrue(all(0 <= used <= 2 for used in capacities))
        ledger = fixture["ledger"]
        events = ledger.events(limit=1000)
        next_reserved = next(event["seq"] for event in events if event["type"] == "run_reserved" and event["task_id"] == "next")
        slow_released = next(event["seq"] for event in events if event["type"] == "resource_released" and event["task_id"] == "slow")
        self.assertLess(next_reserved, slow_released)
        delivered = integration_baseline(fixture["source"])["expected_commit"]
        self.assertIsNotNone(delivered)
        self.assertEqual(fixture["baseline"], git("rev-parse", "HEAD", cwd=fixture["source"]))
        for name in ("slow", "fast", "next"):
            target = ledger.task("fixture", name)
            self.assertEqual("AWAITING_REVIEW", target["state"])
            self.assertTrue({"implementation_verified", "integrated_verified"} <= set(target["gates"]))
            self.assertTrue(all(path.startswith("src/" + name + "/") for path in target["candidate"]["changed_paths"]))
            self.assertEqual(name, git("show", delivered + ":src/" + name + "/result.txt", cwd=fixture["source"]))
        reports = [json.loads(path.read_text()) for path in (fixture["root"] / "pipeline/quality").glob("*/report.json")]
        self.assertEqual(4, len(reports))
        for report in reports:
            self.assertEqual("passed", report["status"])
            self.assertTrue(report["source_unchanged"])
            self.assertEqual("controlled_executor", report["execution_profile"])
            for check in report["checks"]:
                policy = check["sandbox"]["policy"]
                self.assertEqual("none", policy["network"])
                self.assertEqual([], policy["write_scope"])
                self.assertNotIn("provider_transport", policy)
                self.assertNotIn("home_files", policy)
        self.assertEqual(3, len(self.requests), "Only implementation workers may consume provider requests")
        self.assertEqual(1, len(fixture["brokers"]))
        closed = fixture["brokers"][0]
        self.assertTrue(closed._closing)
        self.assertEqual(0, closed.before_close["global_in_flight"])
        self.assertEqual(3, len(closed.before_close["grants"]))
        self.assertTrue(all(grant["status"] == "REVOKED" for grant in closed.before_close["grants"]))
        self.assertEqual({}, fixture["runtime"]._grants)
        for path in (fixture["root"] / "pipeline/prepared").glob("*.json"):
            record = json.loads(path.read_text())
            self.assertNotIn("provider_runtime", record)
            self.assertNotIn("provider_transport", record.get("sandbox_policy", {}))
            self.assertNotIn(SECRET, path.read_text())

    async def test_pipeline_cancel_revokes_grant_and_reuses_the_slot(self):
        self.stream_release.clear()
        fixture = self.pipeline_fixture(["slow", "fast", "next"], auto_integrate=False)
        run = asyncio.create_task(fixture["dispatcher"].run("fixture", timeout=30))
        try:
            await self.await_pipeline_file(fixture, "slow", "src/slow/stream-started")
            cancellation = await fixture["dispatcher"].cancel("fixture", "slow")
            self.assertTrue(cancellation)
            self.assertTrue(all(item["process_tree_terminated"] for item in cancellation))
            result = await asyncio.wait_for(run, 20)
        finally:
            self.stream_release.set()
            if not run.done():
                await fixture["dispatcher"].cancel("fixture")
                await run
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["status"]["capacity"]["used"])
        ledger = fixture["ledger"]
        self.assertEqual("CANCELLED", ledger.task("fixture", "slow")["state"])
        self.assertFalse((fixture["workspaces"]["slow"] / "src/slow/result.txt").exists())
        for name in ("fast", "next"):
            target = ledger.task("fixture", name)
            self.assertIn("implementation_verified", target["gates"])
            self.assertEqual(name, (fixture["workspaces"][name] / ("src/" + name + "/result.txt")).read_text())
        closed = fixture["brokers"][0]
        self.assertEqual(0, closed.before_close["global_in_flight"])
        self.assertTrue(all(grant["status"] == "REVOKED" for grant in closed.before_close["grants"]))
        self.assertEqual({}, fixture["runtime"]._grants)


if __name__ == "__main__":
    unittest.main()
