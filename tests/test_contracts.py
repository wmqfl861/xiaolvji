import copy
import json
from pathlib import Path
import tempfile
import unittest
from xiaolvji.contracts import Fault, digest, load_json, normalize_scope, path_in_scope, scopes_overlap, validate_task
from xiaolvji.authority import Authority

ROOT = Path(__file__).resolve().parents[1]

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.task = json.loads((ROOT / 'examples/task.json').read_text())

    def test_schema_and_defaults_are_not_mutated(self):
        original = copy.deepcopy(self.task)
        self.assertEqual(validate_task(self.task), self.task)
        self.assertEqual(original, self.task)
        self.assertEqual((ROOT/'schemas/task.schema.json').read_bytes(), (ROOT/'src/xiaolvji/resources/task.schema.json').read_bytes())

    def test_closed_schema_and_false_integer(self):
        for change in ({'unknown': 1}, {'schema_version': True}, {'final_review_required': 1}):
            with self.subTest(change=change), self.assertRaises(Fault):
                validate_task(dict(self.task, **change))
        task = copy.deepcopy(self.task)
        task['retry']['max_retries'] = 3
        with self.assertRaises(Fault): validate_task(task)

    def test_duplicate_json_and_nonfinite(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(Fault): load_json(raw)
        self.assertEqual(digest({'b': 1, 'a': 2}), digest({'a': 2, 'b': 1}))

    def test_paths_scope_overlap_and_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            a = normalize_scope(p, [{'path': 'src/a', 'kind': 'directory'}])
            b = normalize_scope(p, [{'path': 'src/a/file', 'kind': 'file'}])
            c = normalize_scope(p, [{'path': 'src/ab', 'kind': 'file'}])
            self.assertTrue(scopes_overlap(a, b))
            self.assertFalse(scopes_overlap(a, c))
            for path in ('../outside', '/outside', '.git/config', 'x/../y', 'x//y', 'x*', 'CON', 'x\\y', 'x:stream'):
                with self.subTest(path=path), self.assertRaises(Fault):
                    normalize_scope(p, [{'path': path, 'kind': 'file'}])
            (p/'alias').symlink_to('/tmp', target_is_directory=True)
            with self.assertRaises(Fault): normalize_scope(p, [{'path':'alias/a','kind':'file'}])
            self.assertFalse(path_in_scope('src/ab', [{'path':'src/a','kind':'directory'}]))

    def test_authorities_distinct_and_key_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            authority = Authority(Path(tmp)/'state')
            result = authority.initialize()
            self.assertEqual(authority.from_file().role, 'operator')
            self.assertEqual(authority.from_file(result['identities']['reviewer']).role, 'reviewer')
            self.assertFalse(authority.initialize()['created'])
            with self.assertRaises(Fault): authority.authenticate('reviewer' * 8)

    def test_invalid_unicode_and_corrupt_authority_fail_explicitly(self):
        with self.assertRaises(Fault): load_json(b'{"x":"\\ud800"}')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'authority.json').write_text('[]')
            with self.assertRaises(Fault) as error: Authority(root).authenticate('x'*48)
            self.assertEqual(error.exception.code, 'UNSAFE_STATE')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'identities').mkdir()
            with self.assertRaises(Fault) as error: Authority(root).initialize()
            self.assertEqual(error.exception.code, 'INSTANCE_UNCERTAIN')

    def test_file_ancestor_and_wrong_case_membership_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'file').write_text('regular')
            with self.assertRaises(Fault): normalize_scope(root, [{'path':'file/child.py','kind':'file'}])
        self.assertFalse(path_in_scope('src/Module.py', [{'path':'src/module.py','kind':'file'}]))
