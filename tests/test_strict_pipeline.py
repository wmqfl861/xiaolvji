"""Real offline OS boundaries across workers, checks, and trusted evidence."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from xiaolvji.check_runner import run_manifest
from xiaolvji.contracts import Fault, Principal, digest
from xiaolvji.dispatcher import Dispatcher
from xiaolvji.executor import ProcessExecutor
from xiaolvji.pipeline import Pipeline
from xiaolvji.quality_service import validate_strict_report
from xiaolvji.store import Ledger
from xiaolvji.workspace import artifact_digest, git


class StrictPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="xiaolvji-strict-probe-") as root:
            probe = ProcessExecutor(Path(root) / "executor", profile="controlled_executor").probe()
        if not probe.get("strict_supported"):
            raise unittest.SkipTest("Real offline Linux sandbox unavailable: " + str(probe.get("blocking_reasons")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaolvji-strict-pipeline-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        for name in ("alpha", "beta"):
            (self.repo / name).mkdir()
            (self.repo / name / "value.txt").write_text("baseline")
        git("init", cwd=self.repo)
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "strict fixture", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        self.state = self.root / "state"
        self.ledger = Ledger(self.state / "ledger.sqlite3", global_limit=2)
        operator = Principal("fixture-operator", "operator")
        self.ledger.enable_project("strict", self.repo, {"project_limit": 2,
            "profile": "controlled_executor", "review_authority": "independent-reviewer"}, operator, "enable")
        self.scopes = [{"path": name, "kind": "directory"} for name in ("alpha", "beta")]
        self.plan = {"id": "strict-plan", "revision": 1, "scope_digest": digest(self.scopes),
            "approved_scope": self.scopes, "approval_ref": "explicit local fixture", "contracts": []}
        self.ledger.register_plan("strict", self.plan, operator, "plan")
        self.commands = {}
        self.executor = ProcessExecutor(self.state / "executor", profile="controlled_executor")
        self.pipeline = Pipeline(self.state / "pipeline", self.ledger,
            command_factory=lambda attempt, workspace, prompt: {
                "argv": [sys.executable, "-B", "-c", self.commands[attempt["task_id"]]], "env": {}})

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, *, command=None, check=None, check_cwd="."):
        self.commands[name] = command or f"from pathlib import Path; Path('{name}/value.txt').write_text('{name}')"
        task = {"schema_version": 1, "task_id": name,
            "plan": {k: self.plan[k] for k in ("id", "revision", "scope_digest")},
            "role": "worker", "engine": "codex", "objective": "Update the declared fixture module",
            "base_commit": self.base, "contract_versions": [], "depends_on": [],
            "write_scope": [{"path": name, "kind": "directory"}], "exclusive_resources": [],
            "acceptance": [{"id": name + "-behavior", "criterion": "Exact fixture value",
                "argv": [sys.executable, "-B", "-c", check or
                    f"from pathlib import Path; assert Path('{name}/value.txt').read_text() == '{name}'"],
                "cwd": check_cwd, "timeout_ms": 6000, "required": True}],
            "expected_artifacts": ["candidate_commit", "validation_report"],
            "retry": {"max_retries": 0, "backoff_ms": 0}, "final_review_required": True}
        self.ledger.register_task("strict", task, Principal("planner", "planner"), "task:" + name)

    async def dispatch(self):
        return await Dispatcher(self.ledger, self.executor, self.pipeline).run("strict", timeout=60)

    async def test_two_scoped_workers_and_isolated_checks_reach_review(self):
        self.add("alpha")
        self.add("beta")
        result = await self.dispatch()
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["status"]["capacity"]["used"])
        for name in ("alpha", "beta"):
            self.assertEqual("AWAITING_REVIEW", self.ledger.task("strict", name)["state"])
            self.assertEqual("baseline", (self.repo / name / "value.txt").read_text())
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))
        reports = list((self.state / "pipeline/quality").glob("*/report.json"))
        self.assertEqual(3, len(reports))
        for path in reports:
            report = json.loads(path.read_text())
            manifest = json.loads(path.with_name("manifest.json").read_text())
            validate_strict_report(report, manifest)
            self.assertTrue(report["source_unchanged"])
            self.assertIsNone(report["environment"]["source_scratch_directory"])
        services = list((self.executor.state_dir / "launches").glob("*/service.json"))
        self.assertEqual(3, len(services))
        self.assertTrue(all(json.loads(p.read_text())["check_profile"] == "controlled_executor" for p in services))

    async def test_worker_cannot_modify_sibling_module_or_original_repository(self):
        self.add("alpha", command="from pathlib import Path; Path('beta/value.txt').write_text('unauthorized')")
        result = await self.dispatch()
        self.assertEqual(0, result["status"]["capacity"]["used"])
        task = self.ledger.task("strict", "alpha")
        self.assertEqual("BLOCKED", task["state"])
        self.assertIsNone(task["candidate"])
        self.assertEqual("baseline", (self.repo / "beta/value.txt").read_text())

    async def test_readonly_check_cannot_change_source_or_receive_gate(self):
        self.add("alpha", check="from pathlib import Path; Path('alpha/value.txt').write_text('forged')")
        result = await self.dispatch()
        self.assertEqual(0, result["status"]["capacity"]["used"])
        task = self.ledger.task("strict", "alpha")
        self.assertEqual("BLOCKED", task["state"])
        self.assertNotIn("implementation_verified", task["gates"])
        report = json.loads(next((self.state / "pipeline/quality").glob("*/report.json")).read_text())
        self.assertEqual("failed", report["status"])
        self.assertTrue(report["source_unchanged"], "The OS should block the attempted write before it changes source")

    async def test_real_host_task_stays_blocked_without_provider_transport(self):
        self.add("alpha")
        self.pipeline.command_factory = None
        result = await self.dispatch()
        self.assertTrue(any(item["error"]["code"] == "CAPABILITY_UNSUPPORTED" for item in result["errors"]))
        self.assertEqual(0, result["status"]["capacity"]["used"])
        self.assertFalse(list((self.executor.state_dir / "launches").iterdir()))
        self.assertIsNone(self.ledger.task("strict", "alpha")["candidate"])

    async def test_invalid_check_preflight_does_not_leave_phantom_capacity(self):
        self.add("alpha", check_cwd="missing-check-directory")
        result = await self.dispatch()
        self.assertTrue(any(item["error"]["code"] == "INVALID_CHECK_MANIFEST" for item in result["errors"]))
        self.assertEqual(0, result["status"]["capacity"]["used"])
        self.assertEqual(0, result["status"]["capacity"]["uncertain"])
        self.assertNotIn("implementation_verified", self.ledger.task("strict", "alpha")["gates"])

    async def test_cancel_during_preflight_prevents_late_worker_start_and_reclaims_slot(self):
        self.add("alpha", command="from pathlib import Path; Path('alpha/value.txt').write_text('must-not-run')")
        entered, release = threading.Event(), threading.Event()
        preflight = self.executor._sandbox_preflight
        def paused_preflight(*args):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("fixture preflight was not released")
            return preflight(*args)
        dispatcher = Dispatcher(self.ledger, self.executor, self.pipeline)
        with patch.object(self.executor, "_sandbox_preflight", paused_preflight):
            running = asyncio.create_task(dispatcher.run("strict", timeout=20))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 8))
                await dispatcher.cancel("strict", "alpha")
                release.set()
                result = await asyncio.wait_for(running, 12)
            finally:
                release.set()
                await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(0, result["status"]["capacity"]["used"])
        self.assertEqual(0, result["status"]["capacity"]["uncertain"])
        self.assertEqual("CANCELLED", self.ledger.task("strict", "alpha")["state"])
        prepared = json.loads(next((self.pipeline.root / "prepared").glob("*.json")).read_text())
        self.assertEqual("baseline", (Path(prepared["workspace"]["path"]) / "alpha/value.txt").read_text())
        self.assertFalse(list((self.executor.state_dir / "launches").glob("*/sandbox.json")),
                         "Cancellation before authorization must not even create the untrusted sandbox worker")


class StrictCheckEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        StrictPipelineTests.setUpClass()

    def test_host_path_executable_cannot_be_silently_replaced_by_sandbox_path(self):
        from tests import test_check_runner as fixtures
        fixture = fixtures.CheckRunnerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        host_bin = fixture.root / "host-bin"
        host_bin.mkdir()
        executable = host_bin / "true"
        executable.write_text("#!/bin/sh\nexit 73\n")
        executable.chmod(0o700)
        check = fixture.check(argv=["true"])
        with patch.dict(os.environ, {"PATH": str(host_bin) + os.pathsep + os.environ.get("PATH", "")}):
            report = run_manifest(fixture.manifest([check]), execution_profile="controlled_executor")
        item = report["checks"][0]
        self.assertEqual("failed", report["status"])
        self.assertEqual(str(executable), item["executable"]["path"])
        self.assertEqual([str(executable)], item["executed_argv"])
        self.assertNotEqual(0, item["exit_code"], "The hidden host tool must not be substituted by /usr/bin/true")

    def test_check_cannot_forge_report_or_read_controller_secret(self):
        with tempfile.TemporaryDirectory(prefix="xiaolvji-strict-evidence-") as root_text:
            root = Path(root_text)
            workspace = root / "source"
            workspace.mkdir()
            (workspace / "value.txt").write_text("source")
            git("init", cwd=workspace)
            git("add", ".", cwd=workspace)
            git("commit", "-m", "strict evidence", cwd=workspace)
            commit = git("rev-parse", "HEAD", cwd=workspace)
            secret = root / "reviewer-fixture.key"
            secret.write_text("synthetic-credential-for-denial-test")
            report_path = root / "report.json"
            forbidden = [str(secret), str(report_path), str(root / "logs/forged.log")]
            script = ("from pathlib import Path\n"
                f"assert not Path({str(secret)!r}).exists()\n"
                f"for name in {forbidden!r}:\n"
                "    try: Path(name).write_text('forged')\n"
                "    except OSError: pass\n"
                "assert Path('value.txt').read_text() == 'source'\n"
                "import tempfile\n"
                "with tempfile.NamedTemporaryFile() as f: f.write(b'private scratch')\n")
            manifest = {"workspace": str(workspace), "checks": [{"id": "boundary", "criterion": "Evidence boundary",
                "argv": [sys.executable, "-B", "-c", script], "timeout_ms": 6000}],
                "artifact": {"commit": commit, "artifact_digest": artifact_digest(workspace / ".git", commit),
                    "plan": {"id": "plan", "revision": 1, "scope_digest": "sha256:" + "1" * 64}, "contracts": []},
                "result_path": str(report_path), "log_dir": str(root / "logs")}
            report = run_manifest(manifest, execution_profile="controlled_executor")
            self.assertEqual("passed", report["status"], report)
            validate_strict_report(report, manifest)
            self.assertEqual("synthetic-credential-for-denial-test", secret.read_text())
            self.assertFalse((root / "logs/forged.log").exists())
            self.assertEqual(report, json.loads(report_path.read_text()))
            self.assertFalse((workspace / ".parallel-dev-check-tmp").exists())
            report["execution_profile"] = "cooperative_native"
            with self.assertRaises(Fault):
                validate_strict_report(report, manifest)
