from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from xiaolvji.contracts import Fault
from xiaolvji.installer import build_plugin, install_policy

REPO = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def test_policy_round_trip_preserves_original_bytes(self):
        original = b"\xef\xbb\xbf# Project instructions\r\nKeep existing quality gates."
        (self.project / "AGENTS.md").write_bytes(original)
        before = install_policy(self.project)
        self.assertIsInstance(before, dict)
        applied = install_policy(self.project, "apply")
        self.assertTrue(applied["writes_performed"])
        installed = (self.project / "AGENTS.md").read_bytes()
        self.assertTrue(installed.startswith(original))
        self.assertFalse(install_policy(self.project, "apply")["writes_performed"])
        install_policy(self.project, "remove")
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), original)

    def test_override_requires_explicit_existing_target(self):
        (self.project / "AGENTS.override.md").write_text("Existing override\n")
        with self.assertRaises(Fault):
            install_policy(self.project, "apply")
        install_policy(self.project, "apply", "AGENTS.override.md")
        self.assertFalse((self.project / "AGENTS.md").exists())
        install_policy(self.project, "remove", "AGENTS.override.md")
        self.assertEqual((self.project / "AGENTS.override.md").read_text(), "Existing override\n")

    def test_user_edit_inside_owned_block_is_not_overwritten(self):
        install_policy(self.project, "apply")
        file = self.project / "AGENTS.md"
        data = file.read_text()
        # Add text inside the managed block while leaving marker ownership intact.
        file.write_text(data.replace("\n", "\nUser-authored change\n", 1))
        changed = file.read_bytes()
        with self.assertRaises(Fault):
            install_policy(self.project, "remove")
        self.assertEqual(file.read_bytes(), changed)

    def fixture_source(self):
        source = self.root / "source"
        (source / "src" / "xiaolvji").mkdir(parents=True)
        (source / "src" / "xiaolvji" / "__init__.py").write_text('__version__ = "9.8.7"\n')
        (source / "src" / "xiaolvji" / "cli.py").write_text('def main():\n    print("fixture-cli-ok")\n    return 0\n')
        for folder, name in (("spec", "AGENTS.block.en.md"), ("spec", "parallel-development.SKILL.md"), ("reference", "inject_agents.py")):
            (source / folder).mkdir(exist_ok=True)
            shutil.copyfile(REPO / folder / name, source / folder / name)
        (source / "docs").mkdir()
        (source / "docs" / "HOST-INSTALL.zh-CN.md").write_text("# Install\n\n[Runtime](RUNTIME.zh-CN.md)\n")
        (source / "docs" / "RUNTIME.zh-CN.md").write_text(
            "[Blueprint](../BLUEPRINT.zh-CN.md) · [Schema](../schemas/task.schema.json) · "
            "[Validation](../VALIDATION.json) · [Tests](../validation/tests.json)\n")
        (source / "BLUEPRINT.zh-CN.md").write_text("# Blueprint\n")
        (source / "schemas").mkdir()
        (source / "schemas" / "task.schema.json").write_text('{"type":"object"}\n')
        (source / "validation").mkdir()
        (source / "validation" / "tests.json").write_text('{"fixture":true}\n')
        (source / "VALIDATION.json").write_text('{"version":"9.8.7","fixture":true}\n')
        return source

    def assert_package_links_resolve(self, destination):
        for document in (*destination.rglob("*.md"), *destination.rglob("*.html")):
            text = document.read_text(encoding="utf-8")
            links = re.findall(r"\]\(([^)\s]+)\)", text)
            links += re.findall(r'(?:href|src)="([^"]+)"', text)
            for link in links:
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                resolved = (document.parent / unquote(parsed.path)).resolve()
                self.assertTrue(resolved.is_relative_to(destination.resolve()), (document, link))
                self.assertTrue(resolved.exists(), (document, link))

    def test_host_packages_have_separate_discovery_paths_and_real_launcher(self):
        source = self.fixture_source()
        for adapter in ("codex", "zcode"):
            destination = self.root / (adapter + " package with spaces")
            result = build_plugin(adapter, destination, source)
            self.assertEqual("9.8.7", json.loads((destination / "PACKAGE.json").read_text())["version"])
            for manifest_path in destination.rglob("plugin.json"):
                self.assertEqual("9.8.7", json.loads(manifest_path.read_text())["version"], manifest_path)
            self.assertFalse(result["installed"])
            self.assertFalse(result["host_integration_tested"])
            self.assertTrue(result["requires_local_host_acceptance"])
            package = json.loads((destination / "PACKAGE.json").read_text())
            self.assertTrue(package["requires_local_host_acceptance"])
            self.assertFalse(package["permissions_granted"])
            self.assertFalse(package["strict_execution_available"])
            self.assertEqual(package["documentation"], "INSTALL.zh-CN.md")
            self.assertEqual(package["supported_native_acceptance_profiles"],
                             ["codex-app-server-v2"] if adapter == "codex" else [])
            install = (destination / "INSTALL.zh-CN.md").read_text()
            self.assertIn("docs/HOST-INSTALL.zh-CN.md", install)
            self.assertIn("完整源码", install)
            self.assertNotEqual(install, (destination / "docs" / "HOST-INSTALL.zh-CN.md").read_text())
            self.assertEqual((destination / "VALIDATION.json").read_bytes(), (source / "VALIDATION.json").read_bytes())
            self.assert_package_links_resolve(destination)
            completed = subprocess.run([sys.executable, str(destination / "bin" / "parallel-dev")],
                                       cwd=self.root, check=True, capture_output=True, text=True)
            self.assertEqual(completed.stdout.strip(), "fixture-cli-ok")
            self.assertTrue((destination / "skills" / "parallel-development" / "SKILL.md").is_file())
            for name in ("AGENTS.block.en.md", "parallel-development.SKILL.md", "inject_agents.py"):
                self.assertTrue((destination / "src" / "xiaolvji" / "resources" / name).is_file())
            hooks_name = "codex.json" if adapter == "codex" else "hooks.json"
            hooks = json.loads((destination / "hooks" / hooks_name).read_text())
            self.assertNotIn("PermissionRequest", hooks["hooks"])
            if adapter == "codex":
                self.assertFalse((destination / "hooks" / "hooks.json").exists())
                self.assertFalse((destination / ".zcode-plugin").exists())
                command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
                self.assertIn('"${PLUGIN_ROOT}/bin/parallel-dev"', command["command"])
                self.assertNotIn(".exe", command["commandWindows"])
            else:
                self.assertFalse((destination / "hooks" / "codex.json").exists())
                self.assertFalse((destination / ".codex-plugin").exists())
                manifest = json.loads((destination / ".zcode-plugin" / "plugin.json").read_text())
                self.assertNotIn("hooks", manifest)
                command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
                self.assertEqual(command["onError"], "deny")
            for record in (destination / "SHA256SUMS").read_text().splitlines():
                expected, name = record.split("  ", 1)
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), expected)

    def test_windows_source_package_uses_python_not_invented_exe(self):
        destination = self.root / "windows-package"
        build_plugin("zcode", destination, self.fixture_source(), target_platform="windows")
        hooks = json.loads((destination / "hooks" / "hooks.json").read_text())
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(command["command"], "py")
        self.assertEqual(command["args"][:2], ["-3", "${ZCODE_PLUGIN_ROOT}/bin/parallel-dev"])
        self.assertFalse(list(destination.rglob("*.exe")))

    def test_nonempty_destination_and_symlink_sources_refused(self):
        source = self.fixture_source()
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "user-file").write_text("retain")
        with self.assertRaises(Fault):
            build_plugin("codex", destination, source)
        self.assertEqual((destination / "user-file").read_text(), "retain")
        (source / "src" / "xiaolvji" / "link").symlink_to(source / "spec")
        with self.assertRaises(Fault):
            build_plugin("codex", self.root / "new", source)
        self.assertFalse((self.root / "new").exists())

    def test_documentation_links_and_new_commands_in_real_runtime_package(self):
        destination = self.root / "complete-runtime"
        build_plugin("codex", destination, REPO)
        self.assert_package_links_resolve(destination)
        for module in ("repository_read.py", "native_policy.py", "native_registry.py", "native_result.py", "codex_app_server.py"):
            self.assertTrue((destination / "src" / "xiaolvji" / module).is_file(), module)
        self.assertFalse((destination / "tests").exists())
        self.assertFalse((destination / "scripts").exists())
        for arguments, expected in ((["host", "--help"], "verify"), (["run", "--help"], "--watch")):
            completed = subprocess.run([sys.executable, str(destination / "bin" / "parallel-dev"), *arguments],
                                       cwd=self.root, check=True, capture_output=True, text=True)
            self.assertIn(expected, completed.stdout)

    def test_documentation_aliases_and_special_files_fail_without_copying(self):
        source = self.fixture_source()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private").write_text("not-package-content")
        for target in (outside, outside / "private"):
            alias = source / "docs" / "alias"
            alias.symlink_to(target)
            try:
                with self.assertRaises(Fault):
                    build_plugin("codex", self.root / "rejected-docs", source)
                self.assertFalse((self.root / "rejected-docs").exists())
            finally:
                alias.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(source / "docs" / "pipe")
            with self.assertRaises(Fault):
                build_plugin("codex", self.root / "rejected-pipe", source)
            self.assertFalse((self.root / "rejected-pipe").exists())

    def test_documentation_limits_and_missing_install_guide_fail_atomically(self):
        source = self.fixture_source()
        for budget in ("MAX_DOCUMENTATION_FILE_BYTES", "MAX_DOCUMENTATION_BYTES", "MAX_DOCUMENTATION_FILES"):
            with self.subTest(budget=budget), mock.patch("xiaolvji.installer." + budget, 1):
                with self.assertRaises(Fault):
                    build_plugin("codex", self.root / "limited", source)
                self.assertFalse((self.root / "limited").exists())
        (source / "docs" / "HOST-INSTALL.zh-CN.md").unlink()
        with self.assertRaises(Fault):
            build_plugin("codex", self.root / "missing-guide", source)
        self.assertFalse((self.root / "missing-guide").exists())

    def test_local_documentation_sidecars_are_not_included(self):
        source = self.fixture_source()
        (source / "docs" / ".local").mkdir()
        (source / "docs" / ".local" / "account.json").write_text('{"private":"not-for-release"}')
        (source / "docs" / ".env").write_text("PRIVATE=not-for-release")
        destination = self.root / "no-sidecars"
        build_plugin("codex", destination, source)
        self.assertFalse((destination / "docs" / ".local").exists())
        self.assertFalse((destination / "docs" / ".env").exists())


if __name__ == "__main__":
    unittest.main()
