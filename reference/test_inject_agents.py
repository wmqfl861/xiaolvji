"""Meaningful safety and round-trip tests for the reference AGENTS injector."""

from pathlib import Path
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from inject_agents import BOM, InjectionError, Injector, STATE_NAME, block_span


class InjectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parallel-dev-injector-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.target = self.project / "AGENTS.md"

    def injector(self, target=None, policy_path=None):
        return Injector(self.project, target=target, policy_path=policy_path)

    def assert_refused_unchanged(self, action, target=None):
        existing = self.target.read_bytes() if self.target.exists() else None
        with self.assertRaises(InjectionError):
            self.injector(target).execute(action)
        observed = self.target.read_bytes() if self.target.exists() else None
        self.assertEqual(existing, observed)

    def test_default_cli_inspect_is_read_only(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("inject_agents.py")), "--project", str(self.project)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "inspect")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_new_file_round_trip_deletes_only_owned_empty_file(self):
        self.assertEqual(self.injector().execute("apply")["status"], "applied")
        self.assertTrue(self.target.exists())
        self.assertEqual(self.injector().execute("remove")["status"], "removed")
        self.assertFalse(self.target.exists())
        self.assertTrue((self.project / ".parallel-dev" / STATE_NAME).exists())
        self.assertEqual(self.injector().execute("remove")["status"], "not_installed")

    def test_bom_crlf_and_existing_bytes_round_trip(self):
        original = BOM + "# Project\r\nKeep café exactly.\r\n".encode("utf-8")
        self.target.write_bytes(original)
        self.injector().execute("apply")
        data = self.target.read_bytes()
        self.assertTrue(data.startswith(original))
        start, end = block_span(data)
        managed = data[start:end]
        self.assertNotIn(b"\n", managed.replace(b"\r\n", b""))
        self.injector().execute("remove")
        self.assertEqual(self.target.read_bytes(), original)

    def test_no_final_newline_is_restored_exactly(self):
        original = b"Original bytes without a final newline"
        self.target.write_bytes(original)
        self.injector().execute("apply")
        self.injector().execute("remove")
        self.assertEqual(self.target.read_bytes(), original)

    def test_existing_empty_and_bom_only_files_remain(self):
        for original in (b"", BOM):
            with self.subTest(original=original):
                self.target.write_bytes(original)
                self.injector().execute("apply")
                self.injector().execute("remove")
                self.assertTrue(self.target.exists())
                self.assertEqual(self.target.read_bytes(), original)

    def test_repeated_apply_preserves_target_manifest_and_backup_count(self):
        self.target.write_bytes(b"Existing guidance\n")
        self.injector().execute("apply")
        manifest = self.project / ".parallel-dev" / STATE_NAME
        saved = (self.target.read_bytes(), manifest.read_bytes())
        count = len(list((self.project / ".parallel-dev" / "backups").iterdir()))
        result = self.injector().execute("apply")
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["writes_performed"])
        self.assertEqual(saved, (self.target.read_bytes(), manifest.read_bytes()))
        self.assertEqual(count, len(list((self.project / ".parallel-dev" / "backups").iterdir())))

    def test_uninstall_preserves_user_edits_outside_block(self):
        self.target.write_bytes(b"Original guidance\n")
        self.injector().execute("apply")
        self.target.write_bytes(b"User introduction\n" + self.target.read_bytes() + b"User footer\n")
        self.injector().execute("remove")
        self.assertEqual(self.target.read_bytes(), b"User introduction\nOriginal guidance\nUser footer\n")

    def test_new_file_with_user_content_is_not_deleted(self):
        self.injector().execute("apply")
        self.target.write_bytes(self.target.read_bytes() + b"User owns this text.\n")
        self.injector().execute("remove")
        self.assertEqual(self.target.read_bytes(), b"User owns this text.\n")

    def test_inside_edits_refuse_apply_and_remove(self):
        self.injector().execute("apply")
        self.target.write_bytes(self.target.read_bytes().replace(b"Follow the host", b"Changed by the user", 1))
        self.assertEqual(self.injector().inspect()["status"], "managed_block_modified")
        self.assert_refused_unchanged("apply")
        self.assert_refused_unchanged("remove")

    def test_duplicate_markers_refuse_mutation(self):
        self.injector().execute("apply")
        self.target.write_bytes(self.target.read_bytes() * 2)
        self.assert_refused_unchanged("apply")
        self.assert_refused_unchanged("remove")

    def test_unowned_block_cannot_be_adopted_or_removed(self):
        self.target.write_bytes(self.injector().policy)
        self.assertEqual(self.injector().inspect()["status"], "unmanaged_block")
        self.assert_refused_unchanged("apply")
        self.assert_refused_unchanged("remove")
        self.assertFalse((self.project / ".parallel-dev").exists())

    def test_override_requires_explicit_existing_target(self):
        override = self.project / "AGENTS.override.md"
        original = b"Existing override instructions\n"
        override.write_bytes(original)
        self.target.write_bytes(b"Root instructions\n")
        self.assertTrue(self.injector().inspect()["warnings"])
        self.assert_refused_unchanged("apply")
        self.assertFalse((self.project / ".parallel-dev").exists())
        self.injector("AGENTS.override.md").execute("apply")
        self.assertEqual(self.target.read_bytes(), b"Root instructions\n")
        self.injector("AGENTS.override.md").execute("remove")
        self.assertEqual(override.read_bytes(), original)

    def test_override_is_never_created(self):
        with self.assertRaises(InjectionError):
            self.injector("AGENTS.override.md").execute("apply")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_case_variant_name_is_rejected(self):
        (self.project / "agents.md").write_bytes(b"Keep this lowercase file\n")
        with self.assertRaises(InjectionError):
            self.injector().execute("apply")
        self.assertEqual((self.project / "agents.md").read_bytes(), b"Keep this lowercase file\n")

    @unittest.skipIf(os.name == "nt", "Distinct case-colliding entries require a case-sensitive test filesystem")
    def test_case_colliding_names_are_rejected(self):
        self.target.write_bytes(b"Upper\n")
        (self.project / "agents.md").write_bytes(b"Lower\n")
        self.assert_refused_unchanged("apply")

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks unavailable")
    def test_symlink_target_is_rejected_without_touching_destination(self):
        outside = self.base / "outside.md"
        outside.write_bytes(b"Outside project\n")
        try:
            self.target.symlink_to(outside)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(InjectionError):
            self.injector().execute("apply")
        self.assertEqual(outside.read_bytes(), b"Outside project\n")

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks unavailable")
    def test_symlink_metadata_directory_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.project / ".parallel-dev").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(InjectionError):
            self.injector().execute("apply")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "Symlinks unavailable")
    def test_symlink_project_is_rejected(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.project, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(InjectionError):
            Injector(alias).execute("apply")
        self.assertEqual(list(self.project.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named pipes unavailable")
    def test_special_target_is_rejected_without_opening_pipe(self):
        os.mkfifo(self.target)
        with self.assertRaises(InjectionError):
            self.injector().execute("apply")
        self.assertTrue(stat.S_ISFIFO(self.target.lstat().st_mode))

    def test_policy_upgrade_replaces_only_owned_block(self):
        original = b"Repository guidance\n"
        self.target.write_bytes(original)
        self.injector().execute("apply")
        self.target.write_bytes(self.target.read_bytes() + b"User footer\n")
        replacement = self.base / "policy.md"
        replacement.write_bytes(self.injector().policy.replace(b'version="1.0.0"', b'version="1.0.1"', 1))
        self.assertEqual(self.injector(policy_path=replacement).execute("apply")["status"], "updated")
        self.assertTrue(self.target.read_bytes().startswith(original))
        self.assertTrue(self.target.read_bytes().endswith(b"User footer\n"))
        self.injector(policy_path=replacement).execute("remove")
        self.assertEqual(self.target.read_bytes(), original + b"User footer\n")

    def test_missing_installed_target_is_not_recreated(self):
        self.injector().execute("apply")
        self.target.unlink()
        with self.assertRaises(InjectionError):
            self.injector().execute("apply")
        self.assertFalse(self.target.exists())

    def test_corrupt_backup_refuses_remove(self):
        self.target.write_bytes(b"Original\n")
        self.injector().execute("apply")
        manifest = json.loads((self.project / ".parallel-dev" / STATE_NAME).read_text())
        backup = self.project / ".parallel-dev" / "backups" / manifest["backup_file"]
        backup.write_bytes(b"Changed backup")
        self.assert_refused_unchanged("remove")

    def test_pending_journal_prevents_automatic_guessing(self):
        self.injector().execute("apply")
        pending = self.project / ".parallel-dev" / "agents-injection.pending.json"
        pending.write_text('{"operation":"interrupted"}\n', encoding="utf-8")
        self.assertEqual(self.injector().inspect()["status"], "interrupted_operation")
        self.assert_refused_unchanged("remove")

    def test_existing_lock_prevents_mutation(self):
        self.target.write_bytes(b"Original\n")
        metadata = self.project / ".parallel-dev"
        metadata.mkdir()
        (metadata / "agents-injection.lock").write_bytes(b"pid=another-operation\n")
        self.assert_refused_unchanged("apply")
        self.assertEqual((metadata / "agents-injection.lock").read_bytes(), b"pid=another-operation\n")

    def test_project_argument_must_be_explicit_absolute_path(self):
        with self.assertRaises(InjectionError):
            Injector(Path("relative-project"))
        with self.assertRaises(InjectionError):
            Injector(self.project / ".." / "project")

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics")
    def test_existing_file_permissions_survive_round_trip(self):
        self.target.write_bytes(b"Original\n")
        self.target.chmod(0o640)
        self.injector().execute("apply")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)
        self.injector().execute("remove")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
