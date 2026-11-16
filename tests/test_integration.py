from pathlib import Path
import tempfile
import unittest

from xiaolvji.contracts import Fault
from xiaolvji.integration import Composer, integration_baseline, publish
from xiaolvji.workspace import Workspaces, artifact_digest, git


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        git("init", cwd=self.repo)
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "b.txt").write_text("b\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "baseline", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        self.manager = Workspaces(self.root / "workers")
        self.composer = Composer(self.root / "combinations")
        self.ref = "refs/heads/parallel-dev/integration"

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, attempt, filename, content):
        workspace = self.manager.create(self.repo, attempt, self.base)
        (Path(workspace["path"]) / filename).write_text(content)
        return self.manager.snapshot(workspace, [{"path": filename, "kind": "file"}])

    def assertFault(self, code, function, *args):
        with self.assertRaises(Fault) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_independent_candidates_combine_and_publish_without_checkout_changes(self):
        first = self.candidate("first", "a.txt", "new a\n")
        second = self.candidate("second", "b.txt", "new b\n")
        combined = self.composer.compose(self.repo, self.base, [first, second])
        self.assertEqual("new a", git("show", f"{combined['commit']}:a.txt", cwd=combined["repository"]))
        self.assertEqual("new b", git("show", f"{combined['commit']}:b.txt", cwd=combined["repository"]))
        self.assertEqual(combined["artifact_digest"], artifact_digest(combined["repository"], combined["commit"]))
        (self.repo / "a.txt").write_text("user in-progress edit\n")
        git("add", "a.txt", cwd=self.repo)
        before_index = git("write-tree", cwd=self.repo)
        initial = integration_baseline(self.repo)
        self.assertIsNone(initial["expected_commit"])
        published = publish(self.repo, self.ref, None, combined)
        self.assertTrue(published["published"])
        self.assertEqual(combined["commit"], git("rev-parse", self.ref, cwd=self.repo))
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(before_index, git("write-tree", cwd=self.repo))
        self.assertEqual("user in-progress edit\n", (self.repo / "a.txt").read_text())

    def test_conflicting_candidates_fail_without_moving_ref(self):
        one = self.candidate("one", "a.txt", "one\n")
        two = self.candidate("two", "a.txt", "two\n")
        self.assertFault("INTEGRATION_CONFLICT", self.composer.compose, self.repo, self.base, [one, two])
        self.assertIsNone(integration_baseline(self.repo)["expected_commit"])
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))

    def test_stale_reference_cannot_publish(self):
        candidate = self.candidate("one", "a.txt", "new\n")
        combined = self.composer.compose(self.repo, self.base, [candidate])
        git("update-ref", self.ref, self.base, cwd=self.repo)
        self.assertFault("REVISION_CONFLICT", publish, self.repo, self.ref, None, combined)
        self.assertEqual(self.base, git("rev-parse", self.ref, cwd=self.repo))

    def test_initial_publication_binds_source_head_atomically(self):
        candidate = self.candidate("one", "a.txt", "new\n")
        combined = self.composer.compose(self.repo, self.base, [candidate])
        (self.repo / "b.txt").write_text("new user commit\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "advanced baseline", cwd=self.repo)
        self.assertFault("REVISION_CONFLICT", publish, self.repo, self.ref, None, combined)
        self.assertIsNone(integration_baseline(self.repo)["expected_commit"])

    def test_subsequent_publication_requires_exact_existing_baseline(self):
        one = self.composer.compose(self.repo, self.base, [self.candidate("one", "a.txt", "new\n")])
        publish(self.repo, self.ref, None, one)
        current = integration_baseline(self.repo)
        two = self.composer.compose(self.repo, current["baseline_commit"], [])
        result = publish(self.repo, self.ref, current["expected_commit"], two)
        self.assertEqual(two["commit"], result["commit"])
        self.assertFault("REVISION_CONFLICT", publish, self.repo, self.ref, one["commit"], dict(two, baseline_commit=self.base))

    def test_unapproved_ref_and_tampered_digest_rejected(self):
        candidate = self.candidate("one", "a.txt", "new\n")
        self.assertFault("ARTIFACT_CHANGED", self.composer.compose, self.repo, self.base,
                         [dict(candidate, artifact_digest="sha256:" + "0" * 64)])
        combined = self.composer.compose(self.repo, self.base, [candidate])
        self.assertFault("INVALID_ARGUMENT", publish, self.repo, "refs/heads/main", None, combined)
        self.assertFault("ARTIFACT_CHANGED", publish, self.repo, self.ref, None,
                         dict(combined, artifact_digest="sha256:" + "0" * 64))

    def test_checked_out_integration_ref_cannot_be_moved(self):
        combined = self.composer.compose(self.repo, self.base, [self.candidate("one", "a.txt", "new\n")])
        git("switch", "-c", "parallel-dev/integration", cwd=self.repo)
        self.assertFault("INTEGRATION_REF_CHECKED_OUT", publish, self.repo, self.ref, self.base, combined)
        self.assertEqual(self.base, git("rev-parse", "HEAD", cwd=self.repo))


if __name__ == "__main__":
    unittest.main()
