"""Authoritative ledger/provider lifecycle with synthetic credentials and no model."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, Principal, digest
from xiaolvji.host_config import render_host_config
from xiaolvji.provider_bridge import BRIDGE_URL
from xiaolvji.provider_broker import ProviderBroker, read_frame, write_frame
from xiaolvji.provider_config import normalize_provider_configuration
from xiaolvji.provider_runtime import ProviderRuntime
from xiaolvji.store import Ledger


SECRET = "synthetic-controller-only-provider-secret"
OP = Principal("provider-operator", "operator")
PLANNER = Principal("provider-planner", "planner")
COORDINATOR = Principal("provider-coordinator", "coordinator")
EXECUTOR = Principal("provider-executor", "executor")


def configuration_value():
    return {"schema_version": 1, "global_request_limit": 2,
            "providers": [{"profile_id": "fixture", "api": "openai-responses",
                           "upstream_url": "https://fixture.invalid/approved/responses",
                           "model": "exact-model", "request_constraints": {"reasoning": {"effort": "ultra"}},
                           "request_timeout": 1}],
            "credential_files": {"fixture": "/operator-only/nonexistent-fixture-key"},
            "bindings": [{"project_id": "project", "host": {
                "schema_version": 1, "engine": "codex", "provider_id": "fixture",
                "api": "openai-responses", "model": "exact-model",
                "quality": {"model_reasoning_effort": "ultra"}}}]}


class NoNetworkBroker(ProviderBroker):
    """Exercise real admission/accounting while refusing every upstream connect."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connect_attempts = 0

    async def _connect_upstream(self, profile):
        self.connect_attempts += 1
        raise OSError("deterministic synthetic connection refusal")


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-provider-runtime-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        (self.repo / "src").mkdir(parents=True)
        self.ledger = Ledger(self.root / "state" / "ledger.sqlite3", global_limit=4)
        self.ledger.enable_project("project", self.repo,
            {"project_limit": 4, "profile": "controlled_executor", "review_authority": "reviewer"}, OP, "enable")
        scope = [{"path": "src", "kind": "directory"}]
        self.plan = {"id": "approved-plan", "revision": 1, "scope_digest": digest(scope),
                     "approved_scope": scope, "approval_ref": "synthetic fixture owner approval", "contracts": []}
        self.ledger.register_plan("project", self.plan, OP, "plan")
        self.sequence = 0
        self.runtime_sequence = 0
        self.runtimes = []
        self.clients = []

    async def asyncTearDown(self):
        for reader, writer in self.clients:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        for runtime in self.runtimes:
            await runtime.close()
        self.temporary.cleanup()

    def key(self):
        self.sequence += 1
        return "provider-test-" + str(self.sequence)

    def attempt(self, task_id="task", *, claimed=True):
        task = {"schema_version": 1, "task_id": task_id,
                "plan": {name: self.plan[name] for name in ("id", "revision", "scope_digest")},
                "role": "worker", "engine": "codex", "objective": "One deterministic provider fixture",
                "base_commit": "1" * 40, "contract_versions": [], "depends_on": [],
                "write_scope": [{"path": "src/" + task_id + ".py", "kind": "file"}],
                "exclusive_resources": [], "acceptance": [{"id": "check", "criterion": "Synthetic check",
                    "argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_ms": 1000, "required": True}],
                "expected_artifacts": ["candidate_commit"],
                "retry": {"max_retries": 0, "backoff_ms": 0}, "final_review_required": True}
        self.ledger.register_task("project", task, PLANNER, self.key())
        attempt = self.ledger.reserve("project", task_id, COORDINATOR, self.key())
        if claimed:
            attempt = self.ledger.claim_launch(attempt["attempt_id"], attempt["generation"], EXECUTOR, self.key())
        return attempt

    def running(self, attempt):
        self.ledger.observe(attempt["attempt_id"], attempt["generation"], "started",
                            {"identity": {"pid": 1001, "boot_id": "synthetic-boot", "start_ticks": "9"}},
                            EXECUTOR, self.key())
        return self.ledger.attempt(attempt["attempt_id"])

    def make_runtime(self, *, config=None, factory=NoNetworkBroker, credentials=None):
        self.runtime_sequence += 1
        configuration = normalize_provider_configuration(config or configuration_value())
        runtime = ProviderRuntime(self.root / "state" / ("provider-" + str(self.runtime_sequence)),
            self.ledger, configuration, broker_factory=factory,
            credentials={"fixture": SECRET} if credentials is None else credentials)
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def command():
        return {"argv": ["/not-executed/codex", "exec", "--json", "--sandbox", "workspace-write",
                         "-c", "agents.enabled=false", "-"], "stdin_text": "fixture prompt", "env": {}}

    def app_server_command(self):
        envelope = {"version": 1, "executable": "/not-executed/codex", "cwd": str(self.repo),
                    "prompt": "Preserve this exact synthetic prompt.\nSecond line.",
                    "model": "exact-model", "effort": "ultra", "argv_extra": []}
        return {"argv": [sys.executable, "-I", "-S", "/home/xiaolvji/.xiaolvji/codex_app_server.py"],
                "stdin_text": json.dumps(envelope), "env": {}, "native_protocol": "codex-app-server-v2"}

    async def client(self, runtime, attempt, bound):
        endpoint = socket.socket(fileno=os.dup(bound["sandbox_additions"]["provider_transport"]["fd"]))
        runtime.handed_off(attempt)
        reader, writer = await asyncio.open_connection(sock=endpoint)
        self.clients.append((reader, writer))
        return reader, writer

    async def request(self, client, bound, request_id):
        reader, writer = client
        await write_frame(writer, {"v": 1, "request_id": request_id,
            "capability": bound["provider_runtime"]["capability"], "method": "POST", "path": "/v1/responses",
            "body": {"model": "exact-model", "reasoning": {"effort": "ultra"}, "input": "synthetic input", "store": False}})
        async with asyncio.timeout(2):
            return await read_frame(reader)

    async def test_reserved_launch_cannot_receive_a_grant(self):
        runtime = self.make_runtime()
        attempt = self.attempt(claimed=False)
        self.assertEqual("RESERVED", attempt["state"])
        self.assertFalse(runtime._admitted(attempt, "grant"))
        with self.assertRaises(Fault) as caught:
            await runtime.bind(attempt, self.command(), 10)
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        self.assertEqual({}, runtime._grants)
        self.assertEqual([], runtime.broker.status()["grants"])

    async def test_claimed_grant_and_running_request_use_actual_ledger_facts(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        self.assertEqual("STARTING", attempt["state"])
        self.assertEqual("CLAIMED", attempt["launch_state"])
        self.assertTrue(runtime._admitted(attempt, "grant"))
        self.assertFalse(runtime._admitted(attempt, "request"))
        bound = await runtime.bind(attempt, self.command(), 10)
        client = await self.client(runtime, attempt, bound)
        early = await self.request(client, bound, "before-running")
        self.assertEqual("BROKER_ADMISSION_DENIED", early["code"])
        self.assertEqual(0, runtime.broker.connect_attempts)
        self.assertEqual(0, runtime.broker.status()["grants"][0]["requests_consumed"])
        current = self.running(attempt)
        self.assertTrue(runtime._admitted(current, "request"))
        self.assertFalse(runtime._admitted(current, "grant"))
        admitted = await self.request(client, bound, "after-running")
        self.assertEqual("BROKER_UPSTREAM_ERROR", admitted["code"])
        self.assertEqual(1, runtime.broker.connect_attempts)
        status = runtime.broker.status()
        self.assertEqual(1, status["grants"][0]["requests_consumed"])
        self.assertEqual("LOCAL_ENDED", status["requests"][0]["state"])

    async def test_only_synthetic_capability_and_digest_evidence_enter_job(self):
        supplied_credentials = {"fixture": SECRET}
        runtime = self.make_runtime(credentials=supplied_credentials)
        attempt = self.attempt()
        command = self.command()
        original = copy.deepcopy(command)
        bound = await runtime.bind(attempt, command, 10)
        self.assertEqual(original, command)
        self.assertEqual(supplied_credentials, {"fixture": SECRET})
        self.assertIsNone(runtime._explicit_credentials)
        self.assertNotIn(SECRET, json.dumps(bound))
        self.assertNotIn("fixture.invalid", json.dumps(bound))
        token = bound["provider_runtime"]["capability"]
        self.assertNotEqual(SECRET, token)
        self.assertEqual(token, bound["provider_runtime"]["env"]["XIAOLVJI_PROVIDER_CAPABILITY"])
        self.assertNotIn(token, json.dumps(bound["provider_binding"]))
        self.assertNotIn(token, json.dumps(bound["command"]))
        self.assertEqual("-", bound["command"]["argv"][-1])
        self.assertIn('model_reasoning_effort="ultra"', bound["command"]["argv"])
        self.assertEqual({}, bound["command"]["env"])
        evidence = bound["provider_binding"]
        self.assertEqual("exact-model", evidence["model"])
        self.assertEqual(runtime.configuration.digest, evidence["configuration_digest"])
        host = runtime.configuration.host("project", "codex")
        self.assertEqual(digest(host), evidence["host_snapshot_digest"])
        self.assertEqual(digest(host["quality"]), evidence["quality_digest"])
        self.assertFalse(evidence["host_evidence"]["runtime_verified"])
        self.assertFalse(evidence["host_evidence"]["real_model_request_tested"])

    async def test_stale_generation_and_launch_identity_cannot_receive_grant(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        for key, value in (("generation", attempt["generation"] + 1), ("launch_key", "wrong-launch")):
            stale = copy.deepcopy(attempt)
            stale[key] = value
            self.assertFalse(runtime._admitted(stale, "grant"))
            with self.subTest(key=key), self.assertRaises(Fault) as caught:
                await runtime.bind(stale, self.command(), 10)
            self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        self.assertIsNone(runtime.broker)

    async def test_other_project_envelope_cannot_select_a_different_provider(self):
        config = configuration_value()
        other = copy.deepcopy(config["providers"][0])
        other["profile_id"] = "other-provider"
        other["model"] = "other-model"
        config["providers"].append(other)
        host = copy.deepcopy(config["bindings"][0]["host"])
        host.update(provider_id="other-provider", model="other-model")
        config["bindings"].append({"project_id": "other-project", "host": host})
        config["credential_files"]["other-provider"] = "/operator-only/other-fixture-key"
        runtime = self.make_runtime(config=config, credentials={"fixture": SECRET, "other-provider": "synthetic-other-key"})
        attempt = self.attempt()
        forged = copy.deepcopy(attempt)
        forged["project_id"] = "other-project"
        with self.assertRaises(Fault) as caught:
            await runtime.bind(forged, self.command(), 10)
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        self.assertIsNone(runtime.broker, "A mismatched envelope must fail before credentials and broker startup")
        forged = copy.deepcopy(attempt)
        forged["task"]["engine"] = "zcode"
        with self.assertRaises(Fault) as caught:
            await runtime.bind(forged, self.command(), 10)
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)

    async def test_cancelled_launch_rejects_stale_pre_cancel_snapshot(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        self.ledger.cancel("project", attempt["task_id"], OP, self.key())
        self.assertFalse(runtime._admitted(attempt, "grant"))
        with self.assertRaises(Fault) as caught:
            await runtime.bind(attempt, self.command(), 10)
        self.assertEqual("BROKER_ADMISSION_DENIED", caught.exception.code)
        self.assertEqual({}, runtime._grants)

    async def test_disabled_project_and_lost_attempt_deny_model_authority(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        self.ledger.observe(attempt["attempt_id"], attempt["generation"], "lost", {"reason": "synthetic uncertainty"}, EXECUTOR, self.key())
        self.assertFalse(runtime._admitted(attempt, "grant"))
        self.assertFalse(runtime._admitted(attempt, "request"))
        with self.assertRaises(Fault):
            await runtime.bind(attempt, self.command(), 10)
        second = self.attempt("second")
        self.ledger.disable_project("project", OP, self.key())
        self.assertFalse(runtime._admitted(second, "grant"))
        with self.assertRaises(Fault):
            await runtime.bind(second, self.command(), 10)
        self.assertEqual({}, runtime._grants)

    async def test_running_cancellation_revokes_channel_without_releasing_ledger_slot(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        bound = await runtime.bind(attempt, self.command(), 10)
        client = await self.client(runtime, attempt, bound)
        self.running(attempt)
        self.ledger.cancel("project", attempt["task_id"], OP, self.key())
        self.assertFalse(runtime._admitted(attempt, "request"))
        async with asyncio.timeout(2):
            self.assertEqual(b"", await client[0].read(1))
        self.assertEqual("REVOKED", runtime.broker.status()["grants"][0]["status"])
        self.assertEqual(1, self.ledger.status()["capacity"]["used"], "Revoked model transport is not process-exit proof")

    async def test_handoff_and_release_are_generation_scoped_and_idempotent(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        bound = await runtime.bind(attempt, self.command(), 10)
        key = (attempt["attempt_id"], attempt["generation"])
        grant = runtime._grants[key]
        client = await self.client(runtime, attempt, bound)
        self.assertEqual(-1, grant.worker_socket.fileno())
        runtime.handed_off(attempt)
        stale = {**attempt, "generation": attempt["generation"] + 1}
        await runtime.release(stale)
        self.assertIn(key, runtime._grants)
        await runtime.release(attempt)
        await runtime.release(attempt)
        self.assertEqual({}, runtime._grants)
        async with asyncio.timeout(2):
            self.assertEqual(b"", await client[0].read(1))
        self.assertEqual("REVOKED", runtime.broker.status()["grants"][0]["status"])
        self.assertEqual("STARTING", self.ledger.attempt(attempt["attempt_id"])["state"])

    async def test_bad_native_command_releases_already_issued_grant(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        with self.assertRaises(Fault) as caught:
            await runtime.bind(attempt, {"argv": ["/not-executed/codex", "exec"], "env": {}}, 10)
        self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)
        self.assertEqual({}, runtime._grants)
        self.assertEqual("REVOKED", runtime.broker.status()["grants"][0]["status"])

    async def test_app_server_binding_inserts_config_into_stdin_and_preserves_runner_argv(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        command = self.app_server_command()
        original = copy.deepcopy(command)
        bound = await runtime.bind(attempt, command, 10)
        self.assertEqual(original, command, "Binding must not mutate the caller's runner command")
        self.assertEqual(original["argv"], bound["command"]["argv"])
        self.assertEqual(original["env"], bound["command"]["env"])
        self.assertEqual("codex-app-server-v2", bound["command"]["native_protocol"])
        envelope = json.loads(bound["command"]["stdin_text"])
        self.assertEqual("exact-model", envelope["model"])
        self.assertEqual("ultra", envelope["effort"])
        host = runtime.configuration.host("project", "codex")
        rendered = render_host_config(host, BRIDGE_URL, bound["provider_runtime"]["capability"])
        self.assertEqual(rendered["argv_extra"], envelope["argv_extra"])
        self.assertIn('model="exact-model"', envelope["argv_extra"])
        self.assertIn('model_reasoning_effort="ultra"', envelope["argv_extra"])
        self.assertEqual(json.loads(original["stdin_text"]), {**envelope, "argv_extra": []})
        self.assertNotIn(SECRET, json.dumps(bound))
        self.assertNotIn(bound["provider_runtime"]["capability"], json.dumps(bound["command"]))
        self.assertEqual(0, runtime.broker.connect_attempts)

    async def assert_app_server_envelope_rejected_and_grant_revoked(self, attempt, stdin_text):
        issued = []
        class CapturingBroker(NoNetworkBroker):
            async def grant(self, *args, **kwargs):
                grant = await super().grant(*args, **kwargs)
                issued.append(grant)
                return grant
        runtime = self.make_runtime(factory=CapturingBroker)
        command = self.app_server_command()
        command["stdin_text"] = stdin_text
        original = copy.deepcopy(command)
        with self.assertRaises(Fault):
            await runtime.bind(attempt, command, 10)
        self.assertEqual(original, command)
        self.assertEqual(1, len(issued), "The test must exercise cleanup after actual grant issuance")
        self.assertEqual(-1, issued[0].worker_socket.fileno())
        self.assertEqual({}, runtime._grants)
        status = runtime.broker.status()
        self.assertEqual(1, len(status["grants"]))
        self.assertEqual("REVOKED", status["grants"][0]["status"])
        self.assertEqual(0, runtime.broker.connect_attempts)
        self.assertEqual("STARTING", self.ledger.attempt(attempt["attempt_id"])["state"])
        self.assertEqual(1, self.ledger.status()["capacity"]["used"], "Provider cleanup is not worker-exit evidence")

    async def test_app_server_mismatched_model_effort_and_existing_overrides_revoke_grant(self):
        attempt = self.attempt()
        baseline = json.loads(self.app_server_command()["stdin_text"])
        cases = (("model", "other-model"), ("model", True), ("model", None),
                 ("effort", "high"), ("effort", True), ("effort", None),
                 ("argv_extra", ["-c", 'model="other-model"']),
                 ("argv_extra", ["-c", 'model="exact-model"']),
                 ("argv_extra", "[]"), ("argv_extra", None), ("argv_extra", {}))
        for key, value in cases:
            with self.subTest(key=key, value=value):
                await self.assert_app_server_envelope_rejected_and_grant_revoked(attempt, json.dumps({**baseline, key: value}))
        for key in ("model", "effort", "argv_extra"):
            with self.subTest(missing=key):
                value = {name: item for name, item in baseline.items() if name != key}
                await self.assert_app_server_envelope_rejected_and_grant_revoked(attempt, json.dumps(value))

    async def test_app_server_malformed_stdin_releases_issued_grant(self):
        attempt = self.attempt()
        for value in (None, "", "{", "null", "[]", '"not an envelope"',
                      '{"model":"exact-model","model":"other","effort":"ultra","argv_extra":[]}'):
            with self.subTest(stdin=value):
                await self.assert_app_server_envelope_rejected_and_grant_revoked(attempt, value)

    async def test_fixture_command_is_preserved_and_close_deauthorizes_every_grant(self):
        runtime = self.make_runtime()
        attempt = self.attempt()
        command = {"argv": [sys.executable, "-c", "pass"], "env": {}, "stdin_text": ""}
        bound = await runtime.bind(attempt, command, 10, native=False)
        self.assertEqual(command, bound["command"])
        grant = runtime._grants[(attempt["attempt_id"], attempt["generation"])]
        await runtime.close()
        await runtime.close()
        self.assertEqual({}, runtime._grants)
        self.assertEqual(-1, grant.worker_socket.fileno())
        self.assertEqual({}, runtime.broker._credentials)
        self.assertIsNone(runtime.broker._db)
        with self.assertRaises(Fault):
            await runtime.start()

    async def test_factory_failure_clears_owned_credential_copy(self):
        captured = []
        original = {"fixture": SECRET}
        def failing_factory(directory, profiles, credentials, callback, **kwargs):
            captured.append(credentials)
            raise Fault("SYNTHETIC_FACTORY_FAILURE", "Synthetic factory failure.")
        runtime = self.make_runtime(factory=failing_factory, credentials=original)
        with self.assertRaises(Fault) as caught:
            await runtime.start()
        self.assertEqual("SYNTHETIC_FACTORY_FAILURE", caught.exception.code)
        self.assertEqual([{}], captured)
        self.assertIsNone(runtime._explicit_credentials)
        self.assertEqual({"fixture": SECRET}, original)
        self.assertIsNone(runtime.broker)

    async def test_close_serializes_with_paused_start_and_leaves_no_live_broker(self):
        entered, release = asyncio.Event(), asyncio.Event()
        constructed = []
        class SlowStartBroker(NoNetworkBroker):
            async def start(self):
                constructed.append(self)
                entered.set()
                await release.wait()
                await super().start()
        runtime = self.make_runtime(factory=SlowStartBroker)
        startup = asyncio.create_task(runtime.start())
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(startup, closing), 2)
        self.assertTrue(runtime._closed)
        self.assertEqual(1, len(constructed))
        self.assertIsNone(constructed[0]._db)
        self.assertEqual({}, constructed[0]._credentials)
        self.assertTrue(constructed[0]._watchdog.done())

    async def test_close_during_grant_handoff_cannot_return_a_live_launch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        issued = []
        class SlowGrantBroker(NoNetworkBroker):
            async def grant(self, *args, **kwargs):
                grant = await super().grant(*args, **kwargs)
                issued.append(grant)
                entered.set()
                await release.wait()
                return grant
        runtime = self.make_runtime(factory=SlowGrantBroker)
        attempt = self.attempt()
        binding = asyncio.create_task(runtime.bind(attempt, self.command(), 10))
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.close()
        release.set()
        with self.assertRaises(Fault):
            await asyncio.wait_for(binding, 2)
        self.assertEqual({}, runtime._grants)
        self.assertEqual(-1, issued[0].worker_socket.fileno())

    async def test_file_loading_receives_every_enrolled_source_and_workspace_root(self):
        configuration = normalize_provider_configuration(configuration_value())
        runtime = ProviderRuntime(self.root / "state" / "provider-explicit-read", self.ledger,
                                  configuration, broker_factory=NoNetworkBroker)
        self.runtimes.append(runtime)
        with patch("xiaolvji.provider_runtime.read_explicit_credentials", return_value={"fixture": SECRET}) as reader:
            await runtime.start()
        self.assertEqual(1, reader.call_count)
        roots = {str(Path(path).resolve()) for path in reader.call_args.kwargs["forbidden_roots"]}
        self.assertIn(str(self.repo.resolve()), roots)
        self.assertIn(str((self.root / "state" / "workspaces").resolve()), roots)


if __name__ == "__main__":
    unittest.main()
