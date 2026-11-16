"""Acceptance-record binding tests; mock reports are not native host evidence."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_provider_runtime import configuration_value
from xiaolvji.authority import atomic_json
from xiaolvji.contracts import Fault, digest, read_json
from xiaolvji.native_acceptance import ASSERTIONS, native_readonly_paths
from xiaolvji.native_policy import normalize_native_deployment
from xiaolvji.native_registry import NativeRegistry
from xiaolvji.provider_config import normalize_provider_configuration


class NativeRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaolvji-native-registry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        self.project.mkdir()
        self.registry = NativeRegistry(self.root / "state")
        self.config = normalize_provider_configuration(configuration_value())
        self.deployment = normalize_native_deployment({"schema_version": 1, "engine": "codex",
            "executable": "/explicit/operator/codex", "protocol": "codex-app-server-v2",
            "policy": {"context": "standalone-api", "system_policy": "must-be-absent", "approval_policy": "on-request"}})
        self.identity = {"fixture": "identity-v1"}
        self.report = {"schema_version": 1, "status": "passed", "kind": "native_protocol_fixture",
            "engine": "codex", "protocol": "codex-app-server-v2", "assertions": {name: True for name in ASSERTIONS},
            "host_snapshot_digest": digest(self.config.host("project", "codex")),
            "deployment_digest": self.deployment.digest, "real_provider_requests": 0,
            "authentication_attempted": False, "native_host_invocations": 1, "synthetic_provider_requests": 2,
            "semantic_error": None, "elapsed_seconds": 1.0, "sandbox_policy_digest": digest("sandbox"),
            "native_result": {"schema_version": 1, "engine": "codex", "status": "completed", "session_id": "fixture-thread",
                "turn_id": None, "event_count": 3, "terminal_type": "turn.completed", "stdout_digest": digest("stdout"),
                "stdout_bytes": 100, "stdout_truncated": False, "process_tree_terminated": False},
            "executor_identity": {"attempt_id": "native-probe", "boot_id": "fixture-boot", "generation": 1,
                "nonce": "fixture-nonce", "pid": 123, "proc_pid": 123, "session_id": 123,
                "pid_namespace": "pid:[123]", "start_time": "456", "profile": "controlled_executor",
                "tracking": "linux_child_subreaper_v1", "sandbox_policy_digest": digest("sandbox")}}
        self.identity_patch = patch("xiaolvji.native_registry.capture_native_identity", return_value=self.identity)
        self.capture = self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.probe_patch = patch("xiaolvji.native_registry.verify_native_host", new_callable=AsyncMock)
        self.probe = self.probe_patch.start()
        self.probe.return_value = self.report
        self.addCleanup(self.probe_patch.stop)

    async def register(self):
        return await self.registry.verify("project", self.project, self.deployment, self.config, self.root / "probe")

    def check(self, **kwargs):
        return self.registry.check("project", "codex", self.project, self.config, **kwargs)

    async def test_record_is_created_only_by_actual_verification_path_and_never_claims_provider_auth(self):
        result = await self.register()
        self.assertTrue(result["registered"])
        self.assertTrue(result["native_protocol_verified"])
        self.assertFalse(result["real_provider_verified"])
        self.probe.assert_awaited_once()
        ready = self.check()
        self.assertEqual(ready["deployment"].value, self.deployment.value)
        self.assertEqual(ready["record_digest"], result["record_digest"])
        self.assertFalse(ready["real_provider_verified"])

    async def test_missing_acceptance_is_not_an_opt_in_boolean(self):
        with self.assertRaises(Fault) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "NATIVE_NOT_VERIFIED")
        self.probe.assert_not_awaited()

    async def test_present_managed_policy_blocks_before_fixture_or_credential_use(self):
        self.capture.side_effect = Fault("CAPABILITY_UNSUPPORTED", "Managed policy cannot be transferred")
        with self.assertRaises(Fault):
            await self.register()
        self.probe.assert_not_awaited()
        self.assertFalse(self.registry.root.exists())

    async def test_every_boundary_assertion_and_its_exact_boolean_are_required(self):
        for name in ASSERTIONS:
            for value in (False, 1, "true"):
                report = copy.deepcopy(self.report)
                report["assertions"][name] = value
                self.probe.return_value = report
                with self.subTest(name=name, value=value), self.assertRaises(Fault) as caught:
                    await self.register()
                self.assertEqual(caught.exception.code, "NATIVE_ACCEPTANCE_FAILED")
        self.assertFalse(self.registry.root.exists())

    async def test_changed_runtime_during_probe_cannot_register(self):
        self.capture.side_effect = [self.identity, {"fixture": "identity-v2"}]
        with self.assertRaises(Fault) as caught:
            await self.register()
        self.assertEqual(caught.exception.code, "NATIVE_IDENTITY_CHANGED")
        self.assertFalse(self.registry.root.exists())

    async def test_report_requires_closed_versioned_typed_native_evidence(self):
        changes = [("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
                   ("native_host_invocations", True), ("native_host_invocations", 0),
                   ("synthetic_provider_requests", 2.0), ("synthetic_provider_requests", 1),
                   ("real_provider_requests", False), ("real_provider_requests", 0.0),
                   ("real_provider_requests", 1), ("native_result", None), ("executor_identity", {}),
                   ("semantic_error", "NATIVE_TURN_FAILED"), ("sandbox_policy_digest", "unbound"),
                   ("elapsed_seconds", True), ("elapsed_seconds", float("inf")),
                   ("elapsed_seconds", 10 ** 400), ("extra", True)]
        for key, value in changes:
            report = copy.deepcopy(self.report)
            report[key] = value
            self.probe.return_value = report
            with self.subTest(key=key, value=value), self.assertRaises(Fault) as caught:
                await self.register()
            self.assertEqual(caught.exception.code, "NATIVE_ACCEPTANCE_FAILED")
        self.assertFalse(self.registry.root.exists())

    async def test_semantic_completion_cannot_claim_tree_proof_or_different_executor(self):
        changes = [("native_result", "process_tree_terminated", True),
                   ("native_result", "stdout_truncated", True), ("native_result", "event_count", True),
                   ("native_result", "terminal_type", "result"), ("native_result", "session_id", ""),
                   ("native_result", "stdout_digest", "unbound"), ("native_result", "stdout_bytes", 0),
                   ("executor_identity", "generation", True), ("executor_identity", "attempt_id", "other"),
                   ("executor_identity", "profile", "cooperative_native"), ("executor_identity", "pid", True),
                   ("executor_identity", "sandbox_policy_digest", digest("other"))]
        for group, key, value in changes:
            report = copy.deepcopy(self.report)
            report[group][key] = value
            self.probe.return_value = report
            with self.subTest(group=group, key=key), self.assertRaises(Fault) as caught:
                await self.register()
            self.assertEqual(caught.exception.code, "NATIVE_ACCEPTANCE_FAILED")
        self.assertFalse(self.registry.root.exists())

    async def test_changed_binary_or_machine_or_runtime_invalidates_existing_record(self):
        await self.register()
        self.capture.return_value = {"fixture": "identity-v2"}
        with self.assertRaises(Fault) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "NATIVE_IDENTITY_CHANGED")

    async def test_changed_provider_binding_needs_reverification(self):
        await self.register()
        value = configuration_value()
        value["providers"][0]["upstream_url"] = "https://changed.invalid/v1/responses"
        self.config = normalize_provider_configuration(value)
        with self.assertRaises(Fault) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "NATIVE_IDENTITY_CHANGED")

    async def test_report_edit_cannot_become_fresh_evidence(self):
        await self.register()
        path = self.registry._path("project", "codex")
        record = read_json(path)
        record["report"]["extra"] = "changed"
        atomic_json(path, record)
        with self.assertRaises(Fault) as caught:
            self.check()
        self.assertEqual(caught.exception.code, "NATIVE_NOT_VERIFIED")

    async def test_normal_code_change_preserves_policy_but_new_policy_blocks(self):
        await self.register()
        (self.project / "code.py").write_text("value = 1\n")
        self.check()
        (self.project / ".codex").mkdir()
        with self.assertRaises(Fault):
            self.check()

    async def test_workspace_policy_is_checked_before_each_launch(self):
        await self.register()
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.check(workspace=workspace)
        (workspace / ".codex").mkdir()
        with self.assertRaises(Fault):
            self.check(workspace=workspace)

    async def test_native_probe_cannot_run_beside_a_dispatcher(self):
        import fcntl
        self.registry.state.mkdir()
        with (self.registry.state / "dispatcher.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Fault) as caught:
                await self.register()
            self.assertEqual(caught.exception.code, "CONTROLLER_RUNNING")
        self.probe.assert_not_awaited()

    def test_native_mount_does_not_duplicate_implicit_toolchain_grant(self):
        with patch("xiaolvji.sandbox.readable_host_sources", return_value=(Path("/usr"), Path("/usr/local"))):
            self.assertEqual(native_readonly_paths("/usr/bin/codex"), [])
            self.assertEqual(native_readonly_paths("/usr/local/bin/codex"), [])
            self.assertEqual(native_readonly_paths("/opt/codex/bin/codex"), ["/opt/codex/bin/codex"])
            self.assertEqual(native_readonly_paths("/usr-not-shared/codex"), ["/usr-not-shared/codex"])


if __name__ == "__main__":
    unittest.main()
