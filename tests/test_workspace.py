import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from xiaolvji.contracts import Fault
from xiaolvji.workspace import Workspaces, artifact_digest, git


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        git("init", cwd=self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src/a.txt").write_text("baseline\n")
        (self.repo / "outside.txt").write_text("keep\n")
        (self.repo / ".gitignore").write_text("*.ignored\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "baseline", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        self.manager = Workspaces(self.root / "workspaces")
        self.workspace = self.manager.create(self.repo, "attempt-one", self.base)
        self.path = Path(self.workspace["path"])
        self.scope = [{"path": "src", "kind": "directory"}]

    def tearDown(self):
        self.temp.cleanup()

    def assertFault(self, code, function, *args):
        with self.assertRaises(Fault) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_private_clones_and_exact_candidate(self):
        (self.path / "src/a.txt").write_text("new behavior\n")
        candidate = self.manager.snapshot(self.workspace, self.scope)
        self.assertEqual(["src/a.txt"], candidate["changed_paths"])
        self.assertEqual(candidate["artifact_digest"], artifact_digest(candidate["repository"], candidate["commit"]))
        self.assertEqual("baseline\n", (self.repo / "src/a.txt").read_text())
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))
        source_object = next((self.repo / ".git/objects").glob("??/*"), None)
        if source_object:
            counterpart = self.path / ".git/objects" / source_object.parent.name / source_object.name
            if counterpart.exists():
                self.assertNotEqual(source_object.stat().st_ino, counterpart.stat().st_ino)

    def test_unleased_tracked_change_rejected(self):
        (self.path / "outside.txt").write_text("bad")
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_ignored_outside_change_is_a_write(self):
        (self.path / "hidden.ignored").write_text("unleased artifact")
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_rename_requires_both_sides(self):
        (self.path / "outside.txt").rename(self.path / "src/moved.txt")
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_deletion_is_a_write(self):
        (self.path / "outside.txt").unlink()
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_symlink_and_special_file_are_rejected(self):
        (self.path / "src/escape").symlink_to(self.repo / "outside.txt")
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)
        (self.path / "src/escape").unlink()
        os.mkfifo(self.path / "src/pipe")
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_worker_git_hooks_and_filters_are_not_executed(self):
        marker = self.root / "evil-executed"
        hook = self.path / ".git/hooks/pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o700)
        git("config", "filter.evil.clean", f"touch '{marker}'", cwd=self.path)
        (self.path / "src/.gitattributes").write_text("*.txt filter=evil\n")
        (self.path / "src/a.txt").write_text("safe capture\n")
        result = self.manager.snapshot(self.workspace, self.scope)
        self.assertTrue(result["commit"])
        self.assertFalse(marker.exists())

    def test_worker_commits_do_not_hide_scope_changes(self):
        (self.path / "outside.txt").write_text("unauthorized")
        git("add", ".", cwd=self.path)
        git("commit", "-m", "worker commit", cwd=self.path)
        self.assertFault("SCOPE_CONFLICT", self.manager.snapshot, self.workspace, self.scope)

    def test_same_tree_has_stable_digest(self):
        first = self.manager.snapshot(self.workspace, [])
        second = self.manager.snapshot(self.workspace, [])
        self.assertEqual(first["artifact_digest"], second["artifact_digest"])
        self.assertEqual([], first["changed_paths"])

    def test_verification_workspace_fetches_candidate_only_ref(self):
        (self.path / "src/a.txt").write_text("candidate only\n")
        candidate = self.manager.snapshot(self.workspace, self.scope)
        verification = self.manager.create(candidate["repository"], "verify-candidate", candidate["commit"])
        self.assertEqual("candidate only\n", (Path(verification["path"]) / "src/a.txt").read_text())
        self.assertEqual(candidate["commit"], git("rev-parse", "HEAD", cwd=verification["path"]))

    def test_duplicate_creation_and_forged_cleanup_rejected(self):
        self.assertFault("INSTANCE_UNCERTAIN", self.manager.create, self.repo, "attempt-one", self.base)
        changed = dict(self.workspace, owner=str(self.repo))
        self.assertFault("WORKSPACE_ERROR", self.manager.remove, changed)
        self.assertTrue(self.repo.exists())
        self.manager.remove(self.workspace)
        self.assertFalse(self.path.exists())

    def test_nested_repo_rejected(self):
        nested = self.path / "src/nested"
        nested.mkdir()
        git("init", cwd=nested)
        self.assertFault("CAPABILITY_UNSUPPORTED", self.manager.snapshot, self.workspace, self.scope)

    def test_baseline_submodules_rejected(self):
        # A gitlink can be constructed without network access or an actual
        # submodule checkout. Its mode alone makes the limitation explicit.
        git("update-index", "--add", "--cacheinfo", "160000", self.base, "nested", cwd=self.repo)
        git("commit", "-m", "gitlink", cwd=self.repo)
        base = git("rev-parse", "HEAD", cwd=self.repo)
        self.assertFault("CAPABILITY_UNSUPPORTED", self.manager.create, self.repo, "with-gitlink", base)


if __name__ == "__main__":
    unittest.main()
