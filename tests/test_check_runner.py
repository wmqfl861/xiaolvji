from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from xiaolvji.check_runner import MAX_LOG_BYTES, run_manifest, validate_manifest
from xiaolvji.contracts import Fault, digest
from xiaolvji.executor import ProcessExecutor


@unittest.skipUnless(sys.platform.startswith("linux"), "Current supervised check runner requires Linux")
class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-check-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Check Fixture")
        self.git("config", "user.email", "check@localhost")
        (self.workspace / "source.txt").write_text("original\n", encoding="utf-8")
        self.git("add", "source.txt")
        self.git("commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        listing = self.git("ls-tree", "-r", "-z", self.commit)
        self.artifact = {"commit": self.commit, "artifact_digest": "sha256:" + hashlib.sha256(listing).hexdigest(),
                         "plan": {"id": "plan-1", "revision": 1, "scope_digest": "sha256:" + "1" * 64}, "contracts": []}
        if not ProcessExecutor(self.root / "probe").probe()["supported"]:
            self.skipTest("Linux process supervision is unavailable")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.workspace, check=True, capture_output=True)
        return result.stdout

    def check(self, code="print('checked')", **extra):
        return {"id": "check-1", "criterion": "The committed source behaves correctly.",
                "argv": [sys.executable, "-c", code], "timeout_ms": 3000, "required": True, **extra}

    def manifest(self, checks=None):
        return {"workspace": str(self.workspace), "checks": checks or [self.check()],
                "artifact": self.artifact, "result_path": str(self.root / "report.json"), "log_dir": str(self.root / "logs")}

    def test_real_success_binds_artifact_commands_environment_and_logs(self):
        manifest = self.manifest()
        report = run_manifest(manifest)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(report["source_before_digest"], report["source_after_digest"])
        self.assertEqual(report["artifact"], self.artifact)
        self.assertEqual(report["manifest_digest"], digest(manifest))
        self.assertEqual(report["environment_digest"], digest(report["environment"]))
        self.assertFalse(report["environment"]["environment_values_recorded"])
        result = report["checks"][0]
        self.assertEqual(result["argv"], manifest["checks"][0]["argv"])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["process_tree_terminated"])
        self.assertEqual(result["status"], "passed")
        for log in result["logs"]:
            path = Path(log["path"])
            self.assertEqual(log["digest"], "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(Path(manifest["result_path"]).read_text()), report)
        with self.assertRaises(Fault):
            run_manifest(manifest)

    def test_nonzero_check_fails_even_without_source_mutation(self):
        report = run_manifest(self.manifest([self.check("import sys; print('failure'); sys.exit(7)")]))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(report["checks"][0]["exit_code"], 7)
        self.assertEqual(report["checks"][0]["status"], "failed")

    def test_cancelled_check_returning_zero_cannot_pass(self):
        outcome = {"identity": {"attempt_id": "fixture"}, "exit_code": 0,
                   "process_tree_terminated": True, "cancelled": True, "timed_out": False}
        with patch("xiaolvji.check_runner.ProcessExecutor.run", new=AsyncMock(return_value=outcome)):
            report = run_manifest(self.manifest())
        self.assertEqual("failed", report["status"])
        self.assertEqual("check_cancelled", report["checks"][0]["reason"])
        self.assertTrue(report["checks"][0]["cancelled"])

    def test_manual_required_criterion_not_run_optional_skipped(self):
        report = run_manifest(self.manifest([
            {"id": "manual", "criterion": "Manual visual check required."},
            {"id": "optional", "criterion": "Optional manual observation.", "required": False},
        ]))
        self.assertEqual(report["status"], "failed")
        self.assertEqual([row["status"] for row in report["checks"]], ["not_run", "skipped"])

    def test_timeout_stops_remaining_checks(self):
        outside_marker = self.root / "must-not-run"
        report = run_manifest(self.manifest([
            self.check("import time; time.sleep(20)", timeout_ms=100),
            self.check(f"from pathlib import Path; Path({str(outside_marker)!r}).write_text('bad')", id="check-2"),
        ]))
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["checks"][0]["timed_out"])
        self.assertTrue(report["checks"][0]["process_tree_terminated"])
        self.assertEqual(report["checks"][1]["status"], "not_run")
        self.assertFalse(outside_marker.exists())

    def test_output_is_bounded_and_marked_truncated(self):
        code = f"import sys; sys.stdout.write('x' * {MAX_LOG_BYTES + 100_000}); sys.stderr.write('done')"
        report = run_manifest(self.manifest([self.check(code)]))
        self.assertEqual(report["status"], "passed")
        output = next(log for log in report["checks"][0]["logs"] if log["stream"] == "stdout")
        self.assertEqual(output["bytes"], MAX_LOG_BYTES)
        self.assertTrue(output["truncated"])

    def test_source_mutation_rejects_pass_and_stops_following_check(self):
        report = run_manifest(self.manifest([
            self.check("from pathlib import Path; Path('source.txt').write_text('changed')"),
            self.check(id="check-2"),
        ]))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["source_unchanged"])
        self.assertEqual(report["checks"][0]["status"], "failed")
        self.assertEqual(report["checks"][1]["status"], "not_run")

    def test_untracked_files_prevent_testing_different_artifact(self):
        (self.workspace / "untracked.txt").write_text("not in candidate")
        report = run_manifest(self.manifest())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][0]["status"], "not_run")
        self.assertFalse(report["source_unchanged"])

    def test_mismatched_candidate_digest_prevents_execution(self):
        manifest = self.manifest()
        manifest["artifact"] = {**self.artifact, "artifact_digest": "sha256:" + "0" * 64}
        report = run_manifest(manifest)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][0]["status"], "not_run")
        self.assertTrue(any(blocker["code"] == "ARTIFACT_MISMATCH" for blocker in report["blockers"]))

    def test_escaped_or_symlink_cwd_and_output_paths_are_rejected(self):
        for cwd in ("../", str(self.root), "path/../"):
            with self.subTest(cwd=cwd), self.assertRaises(Fault):
                validate_manifest(self.manifest([self.check(cwd=cwd)]))
        (self.workspace / "outside").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Fault):
            validate_manifest(self.manifest([self.check(cwd="outside")]))
        manifest = self.manifest()
        manifest["result_path"] = str(self.workspace / "report.json")
        with self.assertRaises(Fault):
            validate_manifest(manifest)

    def test_reserved_scratch_is_allowed_without_source_mutation(self):
        report = run_manifest(self.manifest([self.check(
            "from pathlib import Path; import tempfile; "
            "Path('.parallel-dev-check-tmp/build-output').write_text('generated'); "
            "p=tempfile.NamedTemporaryFile(); p.write(b'bounded temporary work')")]))
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["source_unchanged"])
        self.assertTrue(report["owned_source_scratch_removed"])
        self.assertFalse((self.workspace / ".parallel-dev-check-tmp").exists())

    def test_detached_descendant_terminated_before_next_check(self):
        pid_file = self.root / "daemon.pid"
        first = (
            "import os,time; from pathlib import Path; "
            "pid=os.fork(); "
            f"Path({str(pid_file)!r}).write_text(str(pid)) if pid else None; "
            "os._exit(0) if pid else None; "
            "os.setsid(); time.sleep(30)"
        )
        second = (
            f"from pathlib import Path; import os; pid=int(Path({str(pid_file)!r}).read_text()); "
            "\ntry: os.kill(pid, 0)\nexcept ProcessLookupError: pass\nelse: raise SystemExit(9)"
        )
        report = run_manifest(self.manifest([self.check(first), self.check(second, id="check-2")]))
        self.assertEqual(report["status"], "passed", report)
        self.assertTrue(all(row["process_tree_terminated"] for row in report["checks"]))

    def test_deleted_source_cannot_pass(self):
        report = run_manifest(self.manifest([self.check("from pathlib import Path; Path('source.txt').unlink()")]))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["source_unchanged"])

    def test_escaping_symlink_created_by_check_cannot_pass(self):
        report = run_manifest(self.manifest([self.check("from pathlib import Path; Path('escape').symlink_to('/tmp')")]))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["source_unchanged"])
        self.assertTrue(any(blocker["code"] == "SOURCE_UNSAFE" for blocker in report["blockers"]))

    def test_cli_reads_manifest_and_returns_nonzero_for_failed_check(self):
        manifest = self.manifest([self.check("raise SystemExit(4)")])
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        package_root = Path(__file__).resolve().parents[1] / "src"
        code = "import sys; sys.path.insert(0," + repr(str(package_root)) + "); from xiaolvji.check_runner import main; raise SystemExit(main())"
        run = subprocess.run([sys.executable, "-I", "-c", code, "--manifest", str(path)],
                             cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
