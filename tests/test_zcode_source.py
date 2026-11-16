from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_zcode_source.py"
SPEC = importlib.util.spec_from_file_location("xiaolvji_probe_zcode_source", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class PinnedZCodeSourceTests(unittest.TestCase):
    """A modified checkout must not become executable compatibility evidence."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaolvji-zcode-contract-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git = shutil.which("git")
        if not self.git:
            self.skipTest("Git is required to check pinned source provenance")
        self.env = probe._clean_environment(self.root)
        self.env.update(GIT_AUTHOR_NAME="Synthetic test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Synthetic test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.run_git("-c", "init.defaultBranch=main", "init", "-q")
        for name in probe.SOURCE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export {};\n", encoding="utf-8")
        self.run_git("add", "--all")
        self.run_git("commit", "-q", "--no-gpg-sign", "-m", "Synthetic provenance fixture")
        self.commit = self.run_git("rev-parse", "HEAD").strip()

    def run_git(self, *args):
        return subprocess.run([self.git, *args], cwd=self.root, env=self.env, text=True,
                              capture_output=True, check=True, timeout=10).stdout

    def test_other_commit_is_rejected(self):
        with self.assertRaisesRegex(probe.SourceProbeError, "supported pinned commit"):
            probe.read_pinned_sources(self.root, self.git)

    def test_committed_bytes_are_read_without_mutating_the_checkout(self):
        with mock.patch.object(probe, "ZCODE_SOURCE_COMMIT", self.commit):
            sources = probe.read_pinned_sources(self.root, self.git)
        self.assertEqual(set(probe.SOURCE_FILES), set(sources))
        self.assertEqual({b"export {};\n"}, set(sources.values()))
        self.assertEqual("", self.run_git("status", "--porcelain"))

    def test_modified_source_is_rejected(self):
        for name in (probe.SOURCE_FILES[0], "apps/zcode-cli/packages/cli/src/arguments.ts"):
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(source=name):
                try:
                    path.write_text("throw Error('modified');\n", encoding="utf-8")
                    with mock.patch.object(probe, "ZCODE_SOURCE_COMMIT", self.commit):
                        with self.assertRaisesRegex(probe.SourceProbeError, "committed bytes"):
                            probe.read_pinned_sources(self.root, self.git)
                finally:
                    path.write_bytes(original)

    def test_symlink_to_identical_source_is_rejected(self):
        path = self.root / probe.SOURCE_FILES[0]
        duplicate = self.root / "identical.ts"
        duplicate.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(duplicate)
        except OSError:
            self.skipTest("This filesystem cannot create a test symlink")
        with mock.patch.object(probe, "ZCODE_SOURCE_COMMIT", self.commit):
            with self.assertRaisesRegex(probe.SourceProbeError, "without symlink"):
                probe.read_pinned_sources(self.root, self.git)

    def test_incomplete_sparse_checkout_is_rejected(self):
        (self.root / probe.SOURCE_FILES[0]).unlink()
        with mock.patch.object(probe, "ZCODE_SOURCE_COMMIT", self.commit):
            with self.assertRaisesRegex(probe.SourceProbeError, "sparse checkout"):
                probe.read_pinned_sources(self.root, self.git)


class NativeZCodeSourceIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("XIAOLVJI_ZCODE_SOURCE"),
                         "Set XIAOLVJI_ZCODE_SOURCE to the complete pinned source checkout; native source integration was not run")
    def test_real_pinned_native_compiler_and_argument_parser(self):
        report = probe.probe_source(Path(os.environ["XIAOLVJI_ZCODE_SOURCE"]),
                                    os.environ.get("XIAOLVJI_ZCODE_NODE"))
        self.assertEqual("passed", report["status"])
        self.assertEqual("native_mapping_only", report["evidence_level"])
        self.assertEqual(probe.ZCODE_SOURCE_COMMIT, report["source_commit"])
        self.assertEqual(7, len(report["cases"]))
        self.assertEqual({"openai-responses", "openai-chat-completions", "anthropic-messages"},
                         {case["api"] for case in report["cases"]})
        self.assertTrue(all(case["passed"] for case in report["cases"]))
        self.assertEqual(3, report["negative_controls"])
        self.assertEqual(11, len(report["source_files"]))
        self.assertIn("apps/zcode-cli/packages/cli/src/arguments.ts",
                      {item["path"] for item in report["source_files"]})
        self.assertIs(True, report["argument_parser_tested"])
        argument_cases = {case["name"]: case for case in report["argument_cases"]}
        self.assertEqual({"ordinary", "unicode-multiline", "leading-option", "tool-rule-text",
                          "mode-option-text", "near-ascii-limit", "near-utf8-limit"}, set(argument_cases))
        self.assertTrue(all(case["passed"] for case in argument_cases.values()))
        self.assertEqual(3, report["argument_negative_controls"])
        if os.name != "nt":
            self.assertEqual(120_000, argument_cases["near-ascii-limit"]["prompt_bytes"])
            self.assertEqual(120_000, argument_cases["near-utf8-limit"]["prompt_bytes"])
        for field in ("configuration_schema_loaded", "cli_launched", "real_model_request_tested", "native_execution_ready"):
            self.assertIs(False, report[field])


if __name__ == "__main__":
    unittest.main()
