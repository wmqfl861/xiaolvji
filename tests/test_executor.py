import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, digest
from xiaolvji.executor import ProcessExecutor
from xiaolvji.supervisor import process_identity, signal_identity


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.executor = ProcessExecutor(self.root / "state")
        if not self.executor.probe()["supported"]:
            self.skipTest("Linux subreaper and pidfd are required for process integration tests")

    def tearDown(self):
        self.temp.cleanup()

    async def execute(self, code, attempt="one", **kwargs):
        return await self.executor.run({"attempt_id": attempt, "generation": 1, "launch_key": attempt},
                                       [sys.executable, "-c", code], str(self.work), **kwargs)

    async def await_file(self, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists():
                return path.read_text()
            await asyncio.sleep(0.01)
        self.fail(f"Worker did not produce fixture file: {path.name}")

    async def test_normal_exit_stdin_and_restart_recovery(self):
        result = await self.execute("import sys; print(sys.stdin.read().upper())", stdin_text="hello")
        self.assertTrue(result["process_tree_terminated"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("HELLO\n", Path(result["stdout_path"]).read_text())
        restarted = ProcessExecutor(self.root / "state")
        self.assertEqual(result, restarted.reconcile(result["identity"]))

    async def test_no_execution_before_durable_start_callback(self):
        marker = self.work / "started"
        observed = []
        async def started(identity):
            observed.append(identity)
            self.assertFalse(marker.exists())
            self.assertTrue((self.root / "state/launches/one/identity.json").exists())
            await asyncio.sleep(0.05)
            self.assertFalse(marker.exists())
        result = await self.execute("from pathlib import Path; Path('started').write_text('yes')", on_started=started)
        self.assertTrue(marker.exists())
        self.assertEqual(observed[0], result["identity"])

    async def test_callback_failure_never_launches_worker(self):
        async def failed(identity):
            raise RuntimeError("ledger unavailable")
        with self.assertRaises(RuntimeError):
            await self.execute("from pathlib import Path; Path('forbidden').touch()", on_started=failed)
        self.assertFalse((self.work / "forbidden").exists())
        identity = json.loads((self.root / "state/launches/one/identity.json").read_text())
        result = self.executor.reconcile(identity)
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["no_process_created"])

    async def test_timeout_confirms_full_tree_exit(self):
        result = await self.execute("import time; time.sleep(30)", timeout=0.1)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertNotEqual(0, result["exit_code"])

    async def test_cancel_confirms_full_tree_exit(self):
        task = asyncio.create_task(self.execute("from pathlib import Path; import time; Path('live').touch(); time.sleep(30)"))
        await self.await_file(self.work / "live")
        cancellation = await self.executor.cancel("one", grace=0.05)
        result = await task
        self.assertTrue(cancellation["process_tree_terminated"])
        self.assertTrue(result["cancelled"])

    async def test_detached_double_fork_descendant_is_reaped(self):
        code = """
import os, time
from pathlib import Path
pid = os.fork()
if pid == 0:
    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)
    Path('daemon.pid').write_text(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
os.waitpid(pid, 0)
deadline = time.monotonic() + 2
while not Path('daemon.pid').exists() and time.monotonic() < deadline:
    time.sleep(0.005)
"""
        result = await self.execute(code)
        pid = int((self.work / "daemon.pid").read_text())
        self.assertTrue(result["process_tree_terminated"])
        self.assertIsNone(process_identity(pid))
        self.assertEqual(0, result["exit_code"])

    async def test_missing_supervisor_result_is_uncertain(self):
        identity_ready = asyncio.Future()
        async def started(identity):
            identity_ready.set_result(identity)
        task = asyncio.create_task(self.execute("import os,time; from pathlib import Path; Path('worker.pid').write_text(str(os.getpid())); time.sleep(30)", on_started=started))
        identity = await asyncio.wait_for(identity_ready, timeout=5)
        worker_pid = int(await self.await_file(self.work / "worker.pid"))
        worker = process_identity(worker_pid)
        try:
            signal_identity(identity, signal.SIGKILL)
            result = await task
            self.assertEqual("uncertain", result["status"])
            self.assertFalse(result["process_tree_terminated"])
        finally:
            if worker:
                signal_identity(worker, signal.SIGKILL)

    async def test_recycled_or_forged_identity_never_authorized(self):
        result = await self.execute("pass")
        forged = dict(result["identity"], start_time="0")
        with self.assertRaises(Fault) as caught:
            self.executor.reconcile(forged)
        self.assertEqual("UNKNOWN_IDENTITY", caught.exception.code)

    async def test_process_identity_matches_mounted_proc_namespace(self):
        current = process_identity(os.getpid())
        self.assertIsNotNone(current)
        self.assertEqual(int(os.readlink("/proc/self")), current["proc_pid"])
        self.assertEqual(os.getpid(), current["pid"])

    async def test_concurrent_cancellation_is_idempotent(self):
        task = asyncio.create_task(self.execute("from pathlib import Path; import time; Path('live').touch(); time.sleep(30)"))
        await self.await_file(self.work / "live")
        outcomes = await asyncio.gather(self.executor.cancel("one", grace=0.05), self.executor.cancel("one", grace=0.05))
        self.assertTrue(all(item["process_tree_terminated"] for item in outcomes))
        self.assertTrue((await task)["cancelled"])

    async def test_duplicate_launch_is_refused(self):
        await self.execute("pass")
        with self.assertRaises(Fault) as caught:
            await self.execute("pass")
        self.assertEqual("INSTANCE_UNCERTAIN", caught.exception.code)

    async def test_strict_mode_fails_before_journal_or_process_creation(self):
        strict = ProcessExecutor(self.root / "strict", profile="controlled_executor")
        with patch("xiaolvji.sandbox.LinuxSandbox.probe", return_value={
                "supported": False, "blocking_reasons": ["fixture: kernel namespaces unavailable"]}):
            with self.assertRaises(Fault) as caught:
                await strict.run({"attempt_id": "one", "generation": 1}, [sys.executable, "-c", "pass"], str(self.work))
        self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)
        self.assertEqual([], list((self.root / "strict/launches").iterdir()))

    async def test_controller_and_unrelated_environment_secrets_are_not_inherited(self):
        with patch.dict(os.environ, {"XIAOLVJI_REVIEWER_TOKEN": "sensitive", "UNRELATED_SECRET": "sensitive", "OPENAI_API_KEY": "engine-fixture"}):
            result = await self.execute("import os,json; print(json.dumps({k:os.getenv(k) for k in ['XIAOLVJI_REVIEWER_TOKEN','UNRELATED_SECRET','OPENAI_API_KEY','PARALLEL_DEV_ROLE']}))")
        values = json.loads(Path(result["stdout_path"]).read_text())
        self.assertIsNone(values["XIAOLVJI_REVIEWER_TOKEN"])
        self.assertIsNone(values["UNRELATED_SECRET"])
        self.assertEqual("engine-fixture", values["OPENAI_API_KEY"])
        self.assertEqual("worker", values["PARALLEL_DEV_ROLE"])

    async def test_host_service_credentials_are_not_inherited_with_engine_configuration(self):
        blocked = ["CODEX_APPS_AUTHORIZATION", "CODEX_APPS_FUTURE_TOKEN", "CODEX_CONNECTORS_TOKEN",
                   "CODEX_CONNECTORS_FUTURE_AUTH", "CODEX_AUTH_PATH", "OPENAI_EXECUTOR_API_KEY",
                   "OPENAI_EXECUTOR_FUTURE_TOKEN", "CODEX_FUTURE_SESSION_TOKEN", "OPENAI_FUTURE_SERVICE_SECRET",
                   "ZCODE_CUA_PERMISSION_BROKER_TOKEN", "ANTHROPIC_SESSION_COOKIE", "XDG_UNKNOWN_TOKEN", "LC_UNKNOWN_TOKEN"]
        allowed = {"CODEX_HOME": "/tmp/fixture-only-codex-config", "CODEX_API_KEY": "fixture-only-codex-key",
                   "OPENAI_API_KEY": "fixture-only-provider-key", "OPENAI_BASE_URL": "https://fixture.invalid",
                   "ANTHROPIC_MODEL": "fixture-only-model", "ANTHROPIC_API_KEY": "fixture-only-anthropic-key",
                   "ZCODE_STORAGE_DIR": "/tmp/fixture-only-zcode-config", "ZCODE_MAX_TOOL_CONCURRENCY": "3"}
        synthetic = {**{key: "fixture-only-service-value" for key in blocked}, **allowed}
        # Only synthetic values are printed. No ambient credential value is
        # inspected or serialized by this regression fixture.
        with patch.dict(os.environ, synthetic):
            result = await self.execute("import os,json; print(json.dumps({k:os.getenv(k) for k in " + repr(list(synthetic)) + "}))")
        values = json.loads(Path(result["stdout_path"]).read_text())
        self.assertTrue(all(values[key] is None for key in blocked))
        self.assertEqual(allowed, {key: values[key] for key in allowed})

    async def test_host_service_credentials_cannot_be_added_by_explicit_environment(self):
        blocked = ["CODEX_APPS_AUTHORIZATION", "CODEX_APPS_FUTURE_TOKEN", "CODEX_CONNECTORS_TOKEN",
                   "CODEX_CONNECTORS_FUTURE_AUTH", "CODEX_AUTH_PATH", "OPENAI_EXECUTOR_API_KEY",
                   "OPENAI_EXECUTOR_FUTURE_TOKEN", "CODEX_FUTURE_SESSION_TOKEN", "OPENAI_FUTURE_SERVICE_SECRET",
                   "ZCODE_CUA_PERMISSION_BROKER_TOKEN", "ANTHROPIC_SESSION_COOKIE", "codex_apps_authorization"]
        for index, key in enumerate(blocked):
            with self.subTest(key=key):
                with self.assertRaises(Fault) as caught:
                    await self.execute("raise RuntimeError('must never start')", attempt=f"blocked-{index}",
                                       env={key: "fixture-only-service-value"})
                self.assertEqual("UNAUTHORIZED", caught.exception.code)
        self.assertEqual([], list((self.root / "state/launches").iterdir()))

    async def test_log_output_is_bounded_and_drained(self):
        result = await self.execute("import sys; sys.stdout.write('x' * (5 * 1024 * 1024))")
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(4 * 1024 * 1024, Path(result["stdout_path"]).stat().st_size)
        self.assertTrue(result["log_truncated"]["stdout"])


class StrictExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.executor = ProcessExecutor(self.root / "state", profile="controlled_executor")
        self.capability = self.executor.probe()
        if not self.capability["supported"]:
            self.temp.cleanup()
            self.skipTest("Real Linux offline sandbox is unavailable: " + "; ".join(self.capability["blocking_reasons"]))

    def tearDown(self):
        self.temp.cleanup()

    async def execute(self, code, attempt="one", **kwargs):
        return await self.executor.run({"attempt_id": attempt, "generation": 1, "launch_key": attempt},
                                       [sys.executable, "-c", code], str(self.work), **kwargs)

    async def await_file(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return path.read_text()
            await asyncio.sleep(0.01)
        self.fail("Strict worker did not produce fixture file: " + path.name)

    async def test_authorization_and_durable_kernel_attestation_precede_command(self):
        marker = self.work / "started"
        identities = []
        async def started(identity):
            identities.append(identity)
            self.assertFalse(marker.exists())
            self.assertFalse((self.root / "state/launches/one/worker.json").exists())
            await asyncio.sleep(0.04)
            self.assertFalse(marker.exists())
        result = await self.execute("from pathlib import Path; import sys; Path('started').write_text(sys.stdin.read())",
                                    stdin_text="only worker input\n", on_started=started)
        self.assertEqual(0, result["exit_code"])
        self.assertEqual("only worker input\n", marker.read_text())
        self.assertEqual(identities[0], result["identity"])
        attestation_path = self.root / "state/launches/one/sandbox.json"
        attestation = json.loads(attestation_path.read_text())
        self.assertEqual(attestation, result["sandbox"])
        self.assertTrue(attestation["attested"])
        self.assertEqual(digest(attestation["policy"]), result["identity"]["sandbox_policy_digest"])
        self.assertLessEqual(attestation_path.stat().st_mtime_ns, marker.stat().st_mtime_ns)
        self.assertTrue(attestation["actual"]["controller_paths_hidden"])
        for name, namespace in attestation["actual"]["namespaces"].items():
            self.assertNotEqual(os.readlink("/proc/self/ns/" + name), namespace)
        self.assertEqual("none", attestation["policy"]["network"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        self.assertIsNone(process_identity(attestation["init_identity"]["pid"]))
        # Recovery reads the durable launch profile, including trusted service
        # launches which share this journal root with strict worker launches.
        self.assertEqual(result, ProcessExecutor(self.root / "state").reconcile(result["identity"]))

    async def test_failed_start_callback_never_creates_sandbox_or_worker(self):
        async def failed(identity):
            raise RuntimeError("ledger unavailable")
        with self.assertRaises(RuntimeError):
            await self.execute("from pathlib import Path; Path('forbidden').touch()", on_started=failed)
        self.assertFalse((self.work / "forbidden").exists())
        self.assertFalse((self.root / "state/launches/one/worker.json").exists())
        self.assertFalse((self.root / "state/launches/one/sandbox.json").exists())
        identity = json.loads((self.root / "state/launches/one/identity.json").read_text())
        result = self.executor.reconcile(identity)
        self.assertTrue(result["process_tree_terminated"])
        self.assertFalse(result["worker_started"])

    async def test_exact_write_scope_and_readonly_git_are_enforced(self):
        (self.work / "allowed").mkdir()
        (self.work / "other").mkdir()
        (self.work / "other/data").write_text("original")
        (self.work / ".git").mkdir()
        (self.work / ".git/config").write_text("original git config")
        scope = [{"path": "allowed", "kind": "directory"}]
        code = """
from pathlib import Path
import json, os, tempfile
Path('allowed/output').write_text('yes')
denied = []
for path in ['other/data', '.git/config', 'new-root-file']:
    try:
        Path(path).write_text('forbidden')
    except OSError:
        denied.append(path)
Path(os.environ['HOME'], 'private').write_text('private home')
with tempfile.TemporaryFile() as temporary:
    temporary.write(b'private temp')
print(json.dumps({'denied':denied, 'home':os.environ['HOME']}))
"""
        result = await self.execute(code, sandbox_policy={"workspace": str(self.work), "write_scope": scope})
        self.assertEqual(0, result["exit_code"], Path(result["stderr_path"]).read_text())
        reported = json.loads(Path(result["stdout_path"]).read_text())
        self.assertEqual(["other/data", ".git/config", "new-root-file"], reported["denied"])
        self.assertEqual("/home/xiaolvji", reported["home"])
        self.assertEqual("original", (self.work / "other/data").read_text())
        self.assertEqual("original git config", (self.work / ".git/config").read_text())
        self.assertEqual("yes", (self.work / "allowed/output").read_text())
        self.assertEqual(scope, result["sandbox"]["policy"]["write_scope"])

    async def test_controller_journal_external_files_and_proc_escapes_are_hidden(self):
        (self.root / "hidden").mkdir()
        sentinel = self.root / "hidden/private-controller-data"
        sentinel.write_text("fixture secret")
        controller = self.root / "state/launches/one"
        code = """
import ctypes, errno, json, os
from pathlib import Path
outside = Path(%r)
journal = Path(%r)
denied=[]
for label, action in [
    ('read', lambda: outside.read_text()),
    ('write', lambda: outside.write_text('forged')),
    ('result', lambda: (journal/'result.json').write_text('{}')),
    ('hardlink', lambda: os.link(outside, 'external-hardlink')),
    ('proc-root', lambda: Path('/proc/1/root' + str(outside)).read_text()),
]:
    try:
        action()
    except OSError:
        denied.append(label)
os.symlink(outside, 'external-symlink')
try:
    Path('external-symlink').read_text()
except OSError:
    denied.append('symlink')
libc=ctypes.CDLL(None,use_errno=True)
result=libc.ptrace(0,0,0,0)
print(json.dumps({'denied':denied,'ptrace_denied':result == -1 and ctypes.get_errno()==errno.EPERM}))
""" % (str(sentinel), str(controller))
        result = await self.execute(code)
        self.assertEqual(0, result["exit_code"])
        observed = json.loads(Path(result["stdout_path"]).read_text())
        self.assertEqual(["read", "write", "result", "hardlink", "proc-root", "symlink"], observed["denied"])
        self.assertTrue(observed["ptrace_denied"])
        self.assertEqual("fixture secret", sentinel.read_text())
        self.assertEqual(result["identity"], json.loads((controller / "result.json").read_text())["identity"])

    async def test_host_and_provider_credentials_are_absent_and_network_is_denied(self):
        keys = ["OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ZAI_API_KEY", "HTTPS_PROXY",
                "XIAOLVJI_REVIEWER_TOKEN", "CODEX_APPS_AUTHORIZATION", "UNRELATED_SECRET"]
        synthetic = {key: "fixture-only-secret" for key in keys}
        code = """
import errno,json,os,socket
blocked=[]
for family in [socket.AF_INET, socket.AF_INET6, socket.AF_NETLINK]:
    try:
        sock=socket.socket(family,socket.SOCK_STREAM)
    except OSError as exc:
        blocked.append(exc.errno==errno.EPERM)
    else:
        sock.close()
        blocked.append(False)
print(json.dumps({'env':{key:os.getenv(key) for key in %r},'blocked':blocked,
                  'role':os.getenv('PARALLEL_DEV_ROLE'),'attempt':os.getenv('PARALLEL_DEV_ATTEMPT_ID')}))
""" % keys
        with patch.dict(os.environ, synthetic):
            result = await self.execute(code)
        self.assertEqual(0, result["exit_code"])
        observed = json.loads(Path(result["stdout_path"]).read_text())
        self.assertTrue(all(value is None for value in observed["env"].values()))
        self.assertEqual([True, True, True], observed["blocked"])
        self.assertEqual("worker", observed["role"])
        self.assertEqual("one", observed["attempt"])

    async def test_large_input_control_pipe_is_replaced_before_command(self):
        text = '{"nonce":"worker-data"}\n' * 50000
        result = await self.execute("import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
                                    stdin_text=text)
        import hashlib
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), Path(result["stdout_path"]).read_text().strip())

    async def test_detached_daemon_dies_when_primary_exits(self):
        code = """
import os,time
from pathlib import Path
child=os.fork()
if child==0:
    os.setsid()
    if os.fork():
        os._exit(0)
    Path('daemon-started').touch()
    while True:
        Path('heartbeat').write_text(str(time.monotonic_ns()))
        time.sleep(0.01)
os.waitpid(child,0)
while not Path('daemon-started').exists():
    time.sleep(0.005)
"""
        result = await self.execute(code)
        self.assertEqual(0, result["exit_code"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        last = (self.work / "heartbeat").read_text()
        await asyncio.sleep(0.05)
        self.assertEqual(last, (self.work / "heartbeat").read_text())

    async def test_cancel_and_timeout_confirm_namespace_exit(self):
        code = "import time; from pathlib import Path; Path('live').touch(); time.sleep(30)"
        task = asyncio.create_task(self.execute(code))
        await self.await_file(self.work / "live")
        cancelled = await self.executor.cancel("one", grace=0.05)
        result = await task
        self.assertTrue(cancelled["process_tree_terminated"])
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])
        result = await self.execute("import time; time.sleep(30)", attempt="timeout", timeout=0.2)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["termination_evidence"]["namespace_init_exited"])

    async def test_killed_supervisor_keeps_uncertain_even_if_namespace_dies(self):
        ready = asyncio.Future()
        async def started(identity):
            ready.set_result(identity)
        task = asyncio.create_task(self.execute("import time; from pathlib import Path; Path('live').touch(); time.sleep(30)",
                                                on_started=started))
        identity = await asyncio.wait_for(ready, 5)
        await self.await_file(self.work / "live")
        sandbox = json.loads((self.root / "state/launches/one/sandbox.json").read_text())
        try:
            signal_identity(identity, signal.SIGKILL)
            result = await task
            self.assertEqual("uncertain", result["status"])
            self.assertFalse(result["process_tree_terminated"])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                current = process_identity(sandbox["init_identity"]["pid"])
                if not current or current["state"] == "Z":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Sandbox namespace init survived supervisor death")
            self.assertEqual("uncertain", self.executor.reconcile(identity)["status"])
        finally:
            for key in ("init_identity", "launcher_identity"):
                signal_identity(sandbox[key], signal.SIGKILL)

    async def test_missing_attestation_cannot_be_recovered_as_strict_success(self):
        result = await self.execute("pass")
        (self.root / "state/launches/one/sandbox.json").unlink()
        with self.assertRaises(Fault) as caught:
            self.executor.reconcile(result["identity"])
        self.assertEqual("INSTANCE_UNCERTAIN", caught.exception.code)

    async def test_missing_launch_cannot_downgrade_strict_recovery(self):
        result = await self.execute("pass")
        directory = self.root / "state/launches/one"
        (directory / "launch.json").unlink()
        (directory / "sandbox.json").unlink()
        for executor in (self.executor, ProcessExecutor(self.root / "state")):
            with self.subTest(profile=executor.profile):
                with self.assertRaises(Fault) as caught:
                    executor.reconcile(result["identity"])
                self.assertEqual("INSTANCE_UNCERTAIN", caught.exception.code)

    async def test_launch_profile_nonce_generation_and_policy_are_bound_to_identity(self):
        result = await self.execute("pass")
        path = self.root / "state/launches/one/launch.json"
        original = json.loads(path.read_text())
        mutations = {"profile": "cooperative_native", "attempt_id": "other", "generation": 2,
                     "nonce": "forged", "sandbox_policy_digest": "sha256:" + "0" * 64}
        try:
            for field, value in mutations.items():
                with self.subTest(field=field):
                    path.write_text(json.dumps({**original, field: value}))
                    with self.assertRaises(Fault) as caught:
                        self.executor.reconcile(result["identity"])
                    self.assertEqual("INSTANCE_UNCERTAIN", caught.exception.code)
        finally:
            path.write_text(json.dumps(original))
        self.assertEqual(result, self.executor.reconcile(result["identity"]))

    async def test_invalid_policy_and_explicit_service_credentials_create_no_journal(self):
        with self.assertRaises(Fault) as policy_error:
            await self.execute("raise RuntimeError('must not run')", sandbox_policy={"workspace": str(self.root)})
        self.assertTrue(policy_error.exception.details["no_process_created"])
        with self.assertRaises(Fault) as caught:
            await self.execute("raise RuntimeError('must not run')", env={"CODEX_APPS_AUTHORIZATION": "fixture-only"})
        self.assertEqual("UNAUTHORIZED", caught.exception.code)
        self.assertTrue(caught.exception.details["no_process_created"])
        self.assertEqual([], list((self.root / "state/launches").iterdir()))

    async def test_existing_journal_stays_uncertain_when_capability_disappears(self):
        await self.execute("pass")
        with patch("xiaolvji.sandbox.LinuxSandbox.probe", return_value={
                "supported": False, "blocking_reasons": ["fixture: capability disappeared"]}):
            with self.assertRaises(Fault) as caught:
                await self.execute("raise RuntimeError('must not run')")
        self.assertEqual("INSTANCE_UNCERTAIN", caught.exception.code)
        self.assertFalse((caught.exception.details or {}).get("no_process_created", False))

    async def test_preflight_leaves_dispatch_event_loop_available(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("preflight blocked the event loop")
            return self.capability
        with patch.object(self.executor, "probe", side_effect=probe):
            task = asyncio.create_task(self.execute("pass"))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5))
                release.set()
                result = await task
                self.assertEqual(0, result["exit_code"])
            finally:
                release.set()
                if not task.done():
                    await task

    async def test_cancel_during_preflight_can_reject_authorization_after_identity(self):
        entered, release = threading.Event(), threading.Event()
        prepare = self.executor._sandbox_preflight
        cancellation_requested = False
        identities = []
        def delayed_preflight(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test preflight was not released")
            return prepare(*args)
        async def started(identity):
            # The dispatcher first records this durable identity, then checks
            # its current cancellation/policy state before authorizing a task.
            identities.append(identity)
            self.assertTrue(cancellation_requested)
            self.assertTrue((self.root / "state/launches/one/identity.json").is_file())
            self.assertFalse((self.root / "state/launches/one/worker.json").exists())
            raise Fault("CANCELLED", "Cancelled while strict preflight was in progress")
        with patch.object(self.executor, "_sandbox_preflight", side_effect=delayed_preflight):
            task = asyncio.create_task(self.execute("from pathlib import Path; Path('forbidden').touch()",
                                                    on_started=started))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5))
                cancellation_requested = True
                before_identity = await self.executor.cancel("one", grace=0.05)
                self.assertEqual("uncertain", before_identity["status"])
                self.assertFalse(before_identity["process_tree_terminated"])
                self.assertFalse((self.root / "state/launches/one").exists())
                release.set()
                with self.assertRaises(Fault) as caught:
                    await task
                self.assertEqual("CANCELLED", caught.exception.code)
            finally:
                release.set()
                if not task.done():
                    try:
                        await task
                    except Fault:
                        pass
        self.assertEqual(1, len(identities))
        result = self.executor.reconcile(identities[0])
        self.assertEqual("exited", result["status"])
        self.assertTrue(result["process_tree_terminated"])
        self.assertTrue(result["no_process_created"])
        self.assertFalse(result["worker_started"])
        self.assertNotEqual(0, result["exit_code"])
        self.assertFalse((self.work / "forbidden").exists())
        self.assertFalse((self.root / "state/launches/one/worker.json").exists())
        self.assertFalse((self.root / "state/launches/one/sandbox.json").exists())


if __name__ == "__main__":
    unittest.main()
