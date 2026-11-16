"""Real Git tests for the coordinator's bounded committed-source read surface."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from xiaolvji.contracts import Fault, Principal, digest
from xiaolvji import repository_read as read
from xiaolvji.store import Ledger


READER = Principal("reader", "reader")
OPERATOR = Principal("operator", "operator")
PLANNER = Principal("planner", "planner")


class RepositoryReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaolvji-repo-read-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.defpath, "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Synthetic test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Synthetic test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "module.py").write_text("VALUE = 'committed'\n", encoding="utf-8")
        (self.repo / "src" / "space 中文.txt").write_text("hello 世界\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.key\n", encoding="utf-8")
        (self.repo / "binary.bin").write_bytes(b"abc\x00def")
        (self.repo / "invalid.bin").write_bytes(b"\xff\xfe")
        self.commit = self.commit_files("initial")
        self.ledger = Ledger(self.home / "state" / "ledger.sqlite3")
        self.enable(self.ledger, self.repo)

    def git(self, *args, data=None):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env, input=data,
                                capture_output=True, check=True, timeout=10)
        return result.stdout.decode("utf-8").strip()

    def commit_files(self, message):
        self.git("add", "--all")
        self.git("commit", "-q", "--no-gpg-sign", "-m", message)
        return self.git("rev-parse", "HEAD")

    def enable(self, ledger, repository, plan=True):
        ledger.enable_project("demo", str(repository), {"profile": "controlled_executor", "project_limit": 2,
                              "review_authority": "reviewer"}, OPERATOR, "enable")
        if plan:
            scope = [{"path": "src", "kind": "directory"}]
            ledger.register_plan("demo", {"id": "plan", "revision": 1, "scope_digest": digest(scope),
                                 "approved_scope": scope, "approval_ref": "test:approved", "contracts": []},
                                 PLANNER, "plan")

    def tree(self, **options):
        return read.project_tree(self.ledger, READER, "demo", **options)

    def file(self, path="src/module.py", **options):
        return read.read_file(self.ledger, READER, "demo", path=path,
                              commit=options.pop("commit", self.commit), **options)

    def assert_fault(self, code, operation):
        with self.assertRaises(Fault) as raised:
            operation()
        self.assertEqual(code, raised.exception.code)

    def test_tree_is_pinned_nonrecursive_and_paginated(self):
        first = self.tree(limit=2)
        self.assertEqual(self.commit, first["commit"])
        self.assertEqual(2, first["next_cursor"])
        second = self.tree(commit=first["commit"], cursor=first["next_cursor"], limit=2)
        paths = [item["path"] for item in first["entries"] + second["entries"]]
        self.assertEqual([".gitignore", "binary.bin", "invalid.bin", "src"], paths)
        self.assertIsNone(second["next_cursor"])
        nested = self.tree(directory="src", commit=self.commit)
        self.assertEqual(["src/module.py", "src/space 中文.txt"], [item["path"] for item in nested["entries"]])
        self.assertEqual("tree", second["entries"][-1]["type"])
        self.assertEqual("040000", second["entries"][-1]["mode"])

    def test_reads_commit_bytes_and_never_untracked_or_ignored_files(self):
        (self.repo / "src" / "module.py").write_text("private uncommitted work\n")
        (self.repo / "untracked.key").write_text("synthetic-untracked-secret")
        (self.repo / "ignored.key").write_text("synthetic-ignored-secret")
        result = self.file()
        self.assertEqual("VALUE = 'committed'\n", result["content"])
        self.assertEqual("committed_git", result["source"])
        for path in ("untracked.key", "ignored.key", "../state/ledger.sqlite3", ".git/config"):
            with self.subTest(path=path), self.assertRaises(Fault):
                self.file(path)
        self.assertNotIn("untracked.key", [item["path"] for item in self.tree()["entries"]])

    def test_unicode_bytes_and_executable_mode_are_preserved(self):
        result = self.file("src/space 中文.txt")
        self.assertEqual("hello 世界\n", result["content"])
        self.assertEqual(len(result["content"].encode()), result["bytes"])
        self.git("update-index", "--chmod=+x", "src/module.py")
        self.git("commit", "-q", "--no-gpg-sign", "-m", "executable")
        self.assertEqual("100755", self.file(commit=self.git("rev-parse", "HEAD"))["mode"])

    def test_head_advance_preserves_ancestor_snapshot_reads(self):
        (self.repo / "src" / "module.py").write_text("VALUE = 'new'\n")
        latest = self.commit_files("advance")
        self.assertEqual(self.commit, self.file()["commit"])
        self.assertEqual("VALUE = 'committed'\n", self.file()["content"])
        self.assertEqual("VALUE = 'new'\n", self.file(commit=latest)["content"])

    def test_enrolled_linked_worktree_git_file_is_supported(self):
        worktree = self.home / "linked-worktree"
        self.git("worktree", "add", "--detach", str(worktree), self.commit)
        ledger = Ledger(self.home / "linked-state" / "ledger.sqlite3")
        self.enable(ledger, worktree)
        self.assertTrue((worktree / ".git").is_file())
        result = read.read_file(ledger, READER, "demo", commit=self.commit, path="src/module.py")
        self.assertEqual("VALUE = 'committed'\n", result["content"])

    def test_root_and_git_marker_symlinks_are_rejected(self):
        original = self.home / "original-repo"
        self.repo.rename(original)
        self.repo.symlink_to(original, target_is_directory=True)
        self.assert_fault("REPOSITORY_UNAVAILABLE", self.tree)
        self.repo.unlink()
        original.rename(self.repo)
        marker = self.repo / ".git"
        moved = self.home / "git-metadata"
        marker.rename(moved)
        marker.symlink_to(moved, target_is_directory=True)
        self.assert_fault("REPOSITORY_UNAVAILABLE", self.tree)

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/fd").is_dir(), "Linux descriptor-anchored Git cwd is required")
    def test_replacement_during_git_spawn_cannot_redirect_the_read(self):
        original = self.home / "moved-root"
        replacement = self.home / "replacement"
        replacement.mkdir()
        real_popen = subprocess.Popen
        with read._GitRead(str(self.repo)) as reader:
            descriptor = reader.root_fd
            self.assertIsNotNone(descriptor)
            def raced_spawn(*args, **kwargs):
                self.repo.rename(original)
                self.repo.symlink_to(replacement, target_is_directory=True)
                self.assertEqual(f"/proc/self/fd/{descriptor}", kwargs["cwd"])
                self.assertIn(descriptor, kwargs["pass_fds"])
                return real_popen(*args, **kwargs)
            with mock.patch("xiaolvji.repository_read.subprocess.Popen", side_effect=raced_spawn):
                self.assert_fault("REPOSITORY_UNAVAILABLE", lambda: reader.run(["cat-file", "-t", self.commit], 32))
            self.repo.unlink()
            original.rename(self.repo)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_unrelated_dangling_commit_and_tree_id_are_rejected(self):
        tree = self.git("rev-parse", "HEAD^{tree}")
        dangling = self.git("commit-tree", tree, data=b"unrelated\n")
        self.assert_fault("SCOPE_CONFLICT", lambda: self.file(commit=dangling))
        self.assert_fault("INVALID_INPUT", lambda: self.file(commit=tree))

    def test_force_changed_head_revokes_unreachable_snapshot(self):
        tree = self.git("rev-parse", "HEAD^{tree}")
        other = self.git("commit-tree", tree, data=b"new unrelated root\n")
        self.git("update-ref", "refs/heads/main", other)
        self.assert_fault("SCOPE_CONFLICT", self.file)

    def test_symbols_short_ids_and_invalid_options_are_rejected(self):
        for commit in ("HEAD", self.commit[:12], self.commit.upper(), True):
            with self.subTest(commit=commit):
                self.assert_fault("INVALID_INPUT", lambda: self.file(commit=commit))
        for options in ({"cursor": True}, {"limit": 0}, {"limit": 201}, {"cursor": 1},
                        {"cursor": 1_000_001, "commit": self.commit}):
            with self.subTest(options=options):
                self.assert_fault("INVALID_INPUT", lambda: self.tree(**options))
        for maximum in (True, 0, read.MAX_READ_BYTES + 1):
            self.assert_fault("INVALID_INPUT", lambda: self.file(max_bytes=maximum))

    def test_path_traversal_and_pathspec_magic_cannot_expand_reads(self):
        for path in ("/src/module.py", "../src/module.py", "src/../src/module.py", "src//module.py",
                     "src/./module.py", "src\\module.py", "C:/secret", ".git/config", "a/.parallel-dev/state",
                     "bad\0name", "bad\nname", "x" * 4097):
            with self.subTest(path=path):
                self.assert_fault("INVALID_INPUT", lambda: self.file(path))
        for path in ("src/*.py", ":(glob)src/*.py"):
            self.assert_fault("NOT_FOUND", lambda: self.file(path))

    def test_binary_directory_symlink_and_gitlink_are_not_file_reads(self):
        for path in ("binary.bin", "invalid.bin", "src"):
            self.assert_fault("UNSUPPORTED_FILE", lambda: self.file(path))
        target = self.home / "outside.key"
        target.write_text("synthetic-outside-secret")
        try:
            (self.repo / "link").symlink_to(target)
        except OSError:
            self.skipTest("This filesystem cannot create the symlink negative control")
        current = self.commit_files("link")
        self.assert_fault("UNSUPPORTED_FILE", lambda: self.file("link", commit=current))
        self.git("update-index", "--add", "--cacheinfo", "160000," + current + ",submodule")
        self.git("commit", "-q", "--no-gpg-sign", "-m", "gitlink")
        current = self.git("rev-parse", "HEAD")
        self.assert_fault("UNSUPPORTED_FILE", lambda: self.file("submodule", commit=current))
        self.assert_fault("UNSUPPORTED_FILE", lambda: self.tree(directory="link", commit=current))

    def test_oversized_file_is_rejected_before_blob_body_read(self):
        (self.repo / "large.txt").write_bytes(b"x" * (read.MAX_READ_BYTES + 1))
        current = self.commit_files("large")
        calls = []
        original = read._GitRead.run
        def observed(reader, args, *other, **kw):
            calls.append(args)
            return original(reader, args, *other, **kw)
        with mock.patch.object(read._GitRead, "run", observed):
            self.assert_fault("RESULT_TOO_LARGE", lambda: self.file("large.txt", commit=current, max_bytes=read.MAX_READ_BYTES))
        self.assertFalse(any(args[:2] == ["cat-file", "blob"] for args in calls))

    def test_read_roles_and_enabled_approved_project_are_required(self):
        for principal in (None, Principal("worker", "worker"), Principal("x", "unknown")):
            with self.subTest(principal=principal):
                self.assert_fault("UNAUTHORIZED", lambda: read.project_tree(self.ledger, principal, "demo"))
        self.assert_fault("NOT_FOUND", lambda: read.project_tree(self.ledger, READER, "missing"))
        other = Ledger(self.home / "state2" / "ledger.sqlite3")
        self.enable(other, self.repo, plan=False)
        self.assert_fault("DEPENDENCY_BLOCKED", lambda: read.project_tree(other, READER, "demo"))
        self.ledger.disable_project("demo", OPERATOR, "disable")
        self.assert_fault("PROJECT_DISABLED", self.tree)

    def test_parent_git_discovery_cannot_expand_an_enrolled_subdirectory(self):
        ledger = Ledger(self.home / "state2" / "ledger.sqlite3")
        self.enable(ledger, self.repo / "src")
        self.assert_fault("REPOSITORY_UNAVAILABLE", lambda: read.project_tree(ledger, READER, "demo"))

    def test_git_replacement_objects_do_not_change_committed_content(self):
        original = self.git("rev-parse", "HEAD:src/module.py")
        replacement = self.git("hash-object", "-w", "--stdin", data=b"replacement\n")
        self.git("replace", original, replacement)
        self.assertEqual("VALUE = 'committed'\n", self.file()["content"])

    def test_filters_hooks_fsmonitor_and_pager_are_never_executed(self):
        marker = self.home / "executed"
        script = self.home / "unwanted.sh"
        script.write_text("#!/bin/sh\nprintf bad > " + shlex.quote(str(marker)) + "\n")
        script.chmod(0o755)
        (self.repo / ".gitattributes").write_text("src/module.py filter=unwanted diff=unwanted\n")
        current = self.commit_files("attributes")
        for name in ("core.fsmonitor", "core.pager", "filter.unwanted.smudge", "diff.unwanted.textconv"):
            self.git("config", name, str(script))
        self.git("config", "core.hooksPath", str(self.home))
        self.assertEqual("VALUE = 'committed'\n", self.file(commit=current)["content"])
        self.assertFalse(marker.exists())

    def test_missing_promisor_blob_does_not_launch_fetch_helper(self):
        blob = self.git("rev-parse", "HEAD:src/module.py")
        marker = self.home / "fetch-executed"
        script = self.home / "fetch-helper.sh"
        script.write_text("#!/bin/sh\nprintf fetched > " + shlex.quote(str(marker)) + "\nexit 1\n")
        script.chmod(0o755)
        self.git("config", "extensions.partialClone", "origin")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.url", "ext::" + str(script))
        self.git("config", "protocol.ext.allow", "always")
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        self.assert_fault("REPOSITORY_UNAVAILABLE", self.file)
        self.assertFalse(marker.exists())

    def test_large_directory_first_page_stops_before_scan_budget(self):
        blob = self.git("rev-parse", "HEAD:src/module.py")
        tree_input = "".join(f"100644 blob {blob}\tf{index:05d}\n" for index in range(3000)).encode()
        tree = self.git("mktree", data=tree_input)
        commit = self.git("commit-tree", tree, "-p", self.commit, data=b"large tree\n")
        self.git("update-ref", "refs/heads/main", commit)
        with mock.patch.object(read, "MAX_TREE_SCAN_BYTES", 32_768):
            first = self.tree(limit=2)
        self.assertEqual(["f00000", "f00001"], [item["path"] for item in first["entries"]])
        last = self.tree(commit=commit, cursor=2998, limit=2)
        self.assertEqual(["f02998", "f02999"], [item["path"] for item in last["entries"]])
        self.assertIsNone(last["next_cursor"])

    def test_plan_revision_change_during_read_does_not_publish_result(self):
        ledger = self.ledger
        class ChangingLedger:
            calls = 0
            def project(self, project_id):
                self.calls += 1
                project = copy.deepcopy(ledger.project(project_id))
                if self.calls > 1:
                    project["plan"]["revision"] += 1
                return project
        self.assert_fault("CONTRACT_STALE", lambda: read.read_file(ChangingLedger(), READER, "demo", commit=self.commit, path="src/module.py"))

    @unittest.skipIf(os.name == "nt", "Invalid UTF-8 byte filenames are a POSIX negative control")
    def test_invalid_utf8_tree_names_fail_explicitly(self):
        path = os.fsencode(self.repo) + b"/invalid-\xff"
        with open(path, "wb") as stream:
            stream.write(b"text")
        self.commit_files("invalid path")
        self.assert_fault("UNSUPPORTED_FILE", self.tree)

    def fake_git(self, body):
        script = self.home / "fake-git"
        script.write_text("#!" + sys.executable + "\n" + body)
        script.chmod(0o755)
        reader = read._GitRead(str(self.repo))
        self.addCleanup(reader.home.cleanup)
        reader.git = str(script)
        return reader

    @unittest.skipIf(os.name == "nt", "Executable-script subprocess negative control is POSIX")
    def test_git_output_is_bounded_and_error_text_is_not_disclosed(self):
        reader = self.fake_git("import sys\nsys.stdout.write('x' * 1000000)\nsys.stdout.flush()\n")
        self.assert_fault("RESULT_TOO_LARGE", lambda: reader.run(["status"], 128))
        reader = self.fake_git("import sys\nsys.stderr.write('synthetic-secret-do-not-expose')\nsys.exit(7)\n")
        with self.assertRaises(Fault) as raised:
            reader.run(["status"], 128)
        self.assertNotIn("synthetic-secret", str(raised.exception))

    @unittest.skipIf(os.name == "nt", "Executable-script subprocess negative control is POSIX")
    def test_git_deadline_terminates_a_stalled_read(self):
        reader = self.fake_git("import time\ntime.sleep(30)\n")
        reader.deadline = time.monotonic() + 0.15
        started = time.monotonic()
        self.assert_fault("REPOSITORY_TIMEOUT", lambda: reader.run(["status"], 128))
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
