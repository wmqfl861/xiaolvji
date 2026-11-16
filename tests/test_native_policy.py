from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from xiaolvji.contracts import Fault, digest
from xiaolvji.native_policy import (
    MAX_NATIVE_BYTES, NativeDeployment, capture_native_identity,
    capture_project_policy, load_native_deployment, normalize_native_deployment,
)
from xiaolvji import native_policy


class NativePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-native-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "codex"
        self.executable.write_bytes(self.elf())
        self.executable.chmod(0o755)

    def elf(self, *, machine=62, interpreter=False, dependency=False):
        ident = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
        segments = 1 + int(interpreter) + int(dependency)
        length = 64 + segments * 56 + (32 if dependency else 0)
        header = struct.pack("<16sHHIQQQIHHHHHH", ident, 3, machine, 1, 64,
                             64, 0, 0, 64, 56, segments, 0, 0, 0)
        programs = [struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, length, length, 4096)]
        if interpreter:
            programs.append(struct.pack("<IIQQQQQQ", 3, 4, 0, 0, 0, 0, 0, 1))
        if dependency:
            programs.append(struct.pack("<IIQQQQQQ", 2, 4, 64 + segments * 56,
                                        0, 0, 32, 32, 8))
        return header + b"".join(programs) + (struct.pack("<qQqQ", 1, 7, 0, 0) if dependency else b"")

    def descriptor(self, engine="codex"):
        return {"schema_version": 1, "engine": engine, "executable": str(self.executable),
                "protocol": "codex-app-server-v2" if engine == "codex" else "zcode-cli-v1",
                "policy": {"context": "standalone-api", "system_policy": "must-be-absent",
                           **({"approval_policy": "on-request"} if engine == "codex" else {"mode": "guarded"})}}

    def machine(self):
        return {"system": "Linux", "release": "test-kernel", "architecture": "x86_64",
                "boot_id": "00000000-0000-0000-0000-000000000001"}

    def captured(self, descriptor=None):
        deployment = normalize_native_deployment(descriptor or self.descriptor())
        policy = {"profile": "known-system-sources-absent-v1", "sources": [{"source": "codex_system_policy", "status": "absent"}]}
        with (mock.patch.object(native_policy, "_system_policy_inventory", return_value=policy),
              mock.patch.object(native_policy, "_machine_identity", return_value=self.machine()),
              mock.patch.object(native_policy, "_runtime_identity", return_value={"sha256": "sha256:" + "b" * 64, "files": 1}),
              mock.patch.object(native_policy, "_tool_identity", side_effect=lambda name, machine: {"path": "/usr/bin/" + name, "sha256": "sha256:" + "c" * 64, "version": "fixture"})):
            return capture_native_identity(deployment)

    def file_identity(self, **kwargs):
        return native_policy._file_identity(self.executable, maximum=MAX_NATIVE_BYTES,
            label="Native host executable", machine="x86_64", require_static=True, **kwargs)

    def test_descriptors_are_closed_and_immutable_copies(self):
        for engine in ("codex", "zcode"):
            value = self.descriptor(engine)
            deployment = normalize_native_deployment(value)
            baseline = deployment.value
            value["policy"]["context"] = "changed"
            another = deployment.value
            another["policy"]["system_policy"] = "ignore"
            self.assertEqual(baseline, deployment.value)
            self.assertEqual(digest(baseline), deployment.digest)
            self.assertNotIn(str(self.executable), repr(deployment))

    def test_unknown_fields_and_claimed_attestation_are_rejected(self):
        for key in ("native_verified", "attestation", "credential", "environment", "policy_paths"):
            value = self.descriptor()
            value[key] = True
            with self.subTest(key=key), self.assertRaises(Fault) as raised:
                normalize_native_deployment(value)
            self.assertEqual("INVALID_NATIVE_DEPLOYMENT", raised.exception.code)
        value = self.descriptor()
        value["policy"]["requirements_verified"] = True
        with self.assertRaises(Fault):
            normalize_native_deployment(value)

    def test_invalid_types_and_engine_protocol_combinations_fail(self):
        cases = [("schema_version", True), ("schema_version", 2), ("engine", []),
                 ("engine", "unknown"), ("protocol", "zcode-cli-v1"), ("protocol", True),
                 ("policy", []), ("policy", None)]
        for key, bad in cases:
            value = self.descriptor()
            value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(Fault):
                normalize_native_deployment(value)
        for bad in ({}, [], None, "text"):
            with self.subTest(bad=bad), self.assertRaises(Fault):
                normalize_native_deployment(bad)

    def test_implicit_or_unsafe_deployment_policies_fail(self):
        for key, value in (("context", "inherit-account"), ("context", "automatic"),
                           ("system_policy", "ignore"), ("approval_policy", "never"),
                           ("approval_policy", "untrusted")):
            spec = self.descriptor()
            spec["policy"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Fault):
                normalize_native_deployment(spec)
        spec = self.descriptor("zcode")
        spec["policy"]["mode"] = "yolo"
        with self.assertRaises(Fault):
            normalize_native_deployment(spec)

    def test_executable_requires_explicit_canonical_absolute_path(self):
        for value in ("codex", "./codex", "/a/../codex", "/a//codex", "/a/./codex",
                      "/a/codex/", "/", "\x00", 1, "/a/\ud800", "/" + "x" * 4097):
            spec = self.descriptor()
            spec["executable"] = value
            with self.subTest(value=repr(value)[:60]), self.assertRaises(Fault):
                normalize_native_deployment(spec)

    def test_normalization_reads_no_files_and_executes_nothing(self):
        with (mock.patch("builtins.open") as opened,
              mock.patch.object(native_policy.os, "open") as os_open,
              mock.patch.object(native_policy.subprocess, "run") as run):
            normalize_native_deployment(self.descriptor())
        opened.assert_not_called()
        os_open.assert_not_called()
        run.assert_not_called()

    def test_load_uses_only_explicit_regular_descriptor(self):
        path = self.root / "native.json"
        value = self.descriptor()
        path.write_text(json.dumps(value))
        self.assertEqual(value, load_native_deployment(path).value)
        link = self.root / "linked.json"
        link.symlink_to(path)
        with self.assertRaises(Fault):
            load_native_deployment(link)
        path.write_text('{"engine":"codex","engine":"zcode"}')
        with self.assertRaises(Fault) as raised:
            load_native_deployment(path)
        self.assertEqual("INVALID_NATIVE_DEPLOYMENT", raised.exception.code)

    def test_descriptor_load_is_bounded_and_nonregular_inputs_fail(self):
        path = self.root / "native.json"
        path.write_bytes(b" " * 65_537)
        with self.assertRaises(Fault):
            load_native_deployment(path)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for value in (fifo, self.root, self.root / "missing"):
            with self.subTest(value=value.name), self.assertRaises(Fault):
                load_native_deployment(value)

    def test_system_policy_presence_blocks_without_reading_contents(self):
        path = self.root / "policy"
        cases = ("file", "directory", "symlink")
        for kind in cases:
            if kind == "file":
                path.write_text("PRIVATE_POLICY_CONTENTS")
            elif kind == "directory":
                path.mkdir()
            else:
                path.symlink_to(self.root / "missing")
            with (mock.patch.object(native_policy, "_CODEX_POLICY_ROOTS", (("fixture", path),)),
                  mock.patch("builtins.open") as opened,
                  mock.patch.object(Path, "iterdir") as listed):
                with self.assertRaises(Fault) as raised:
                    native_policy._system_policy_inventory("codex")
                self.assertNotIn("PRIVATE_POLICY_CONTENTS", str(raised.exception))
                opened.assert_not_called()
                listed.assert_not_called()
            if kind == "directory":
                path.rmdir()
            else:
                path.unlink()

    def test_system_policy_absence_is_metadata_only(self):
        path = self.root / "absent"
        with (mock.patch.object(native_policy, "_CODEX_POLICY_ROOTS", (("fixture", path),)),
              mock.patch("builtins.open") as opened,
              mock.patch.object(Path, "iterdir") as listed):
            result = native_policy._system_policy_inventory("codex")
        self.assertEqual([{"source": "fixture", "status": "absent"}], result["sources"])
        opened.assert_not_called()
        listed.assert_not_called()

    def test_policy_parent_alias_and_inspection_errors_block(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(native_policy, "_CODEX_POLICY_ROOTS", (("fixture", alias / "policy"),)):
            with self.assertRaises(Fault):
                native_policy._system_policy_inventory("codex")
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("PRIVATE_DETAILS")):
            with self.assertRaises(Fault) as raised:
                native_policy._system_policy_inventory("codex")
        self.assertNotIn("PRIVATE_DETAILS", str(raised.exception))

    def test_policy_is_checked_before_native_file_reads(self):
        deployment = normalize_native_deployment(self.descriptor())
        with (mock.patch.object(native_policy, "_system_policy_inventory", side_effect=Fault("CAPABILITY_UNSUPPORTED", "blocked")),
              mock.patch.object(native_policy, "_file_identity") as files):
            with self.assertRaises(Fault):
                capture_native_identity(deployment)
        files.assert_not_called()

    def test_identity_binds_exact_static_elf_without_running_host(self):
        with mock.patch.object(native_policy.subprocess, "run") as run:
            result = self.captured()
        self.assertEqual(str(self.executable), result["executable"]["path"])
        self.assertEqual("sha256:" + hashlib.sha256(self.executable.read_bytes()).hexdigest(), result["executable"]["sha256"])
        self.assertTrue(result["executable"]["static_runtime"])
        self.assertEqual(digest({key: value for key, value in result.items() if key != "identity_digest"}), result["identity_digest"])
        self.assertNotIn("ready", result)
        self.assertNotIn("verified", result)
        run.assert_not_called()

    def test_wrapper_dynamic_and_wrong_architecture_are_unsupported(self):
        for data in (b"#!/bin/sh\necho wrapper\n", self.elf(interpreter=True),
                     self.elf(dependency=True), self.elf(machine=183), b"\x7fELF"):
            self.executable.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(Fault):
                self.file_identity()

    def test_malformed_elf_file_ranges_fail(self):
        data = bytearray(self.elf())
        # e_phoff outside the actual file cannot be treated as a static binary.
        data[32:40] = struct.pack("<Q", 1_000_000)
        self.executable.write_bytes(data)
        with self.assertRaises(Fault):
            self.file_identity()
        data = bytearray(self.elf())
        data[64 + 32:64 + 40] = struct.pack("<Q", 1_000_000)
        self.executable.write_bytes(data)
        with self.assertRaises(Fault):
            self.file_identity()

    def test_native_permissions_and_path_aliases_fail(self):
        for mode in (0o644, 0o777, 0o4755, 0o2755):
            self.executable.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(Fault):
                self.file_identity()
        self.executable.chmod(0o755)
        alias = self.root / "alias"
        alias.symlink_to(self.executable)
        with self.assertRaises(Fault):
            native_policy._file_identity(alias, maximum=MAX_NATIVE_BYTES, label="Native host executable", machine="x86_64", require_static=True)

    def test_native_hash_capture_is_bounded_and_detects_changes(self):
        with self.executable.open("wb") as stream:
            stream.truncate(MAX_NATIVE_BYTES + 1)
        with self.assertRaises(Fault):
            self.file_identity()
        self.executable.write_bytes(self.elf())
        before = self.executable.stat()
        after = SimpleNamespace(**{key: getattr(before, key) for key in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")})
        after.st_ctime_ns += 1
        with mock.patch.object(native_policy.os, "fstat", side_effect=[before, after]):
            with self.assertRaises(Fault) as raised:
                self.file_identity()
        self.assertIn("changed", str(raised.exception))

    def test_zcode_is_explicitly_unsupported_not_partial_hash_verified(self):
        deployment = normalize_native_deployment(self.descriptor("zcode"))
        with (mock.patch.object(native_policy, "_file_identity") as files,
              mock.patch.object(native_policy, "_system_policy_inventory") as policies):
            with self.assertRaises(Fault) as raised:
                capture_native_identity(deployment)
        self.assertEqual("CAPABILITY_UNSUPPORTED", raised.exception.code)
        self.assertIn("complete runtime distribution", str(raised.exception))
        files.assert_not_called()
        policies.assert_not_called()

    def test_forged_dataclass_is_revalidated(self):
        value = self.descriptor()
        value["policy"]["approval_policy"] = "never"
        for invalid in (value, NativeDeployment(json.dumps(value)), NativeDeployment(1)):
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(Fault):
                capture_native_identity(invalid)

    def test_identity_changes_with_binary_deployment_runtime_or_machine(self):
        baseline = self.captured()["identity_digest"]
        self.executable.write_bytes(self.elf() + b"changed")
        self.assertNotEqual(baseline, self.captured()["identity_digest"])
        with (mock.patch.object(native_policy, "_machine_identity", return_value={**self.machine(), "boot_id": "00000000-0000-0000-0000-000000000002"}),
              mock.patch.object(native_policy, "_system_policy_inventory", return_value={"sources": []}),
              mock.patch.object(native_policy, "_runtime_identity", return_value={"sha256": "changed"}),
              mock.patch.object(native_policy, "_tool_identity", return_value={"sha256": "tool"})):
            self.assertNotEqual(baseline, capture_native_identity(normalize_native_deployment(self.descriptor()))["identity_digest"])

    def test_runtime_identity_includes_python_and_all_resources(self):
        root = self.root / "runtime"
        (root / "resources").mkdir(parents=True)
        (root / "module.py").write_text("value=1\n")
        (root / "__init__.py").write_text("")
        (root / "resources" / "policy.md").write_text("policy one\n")
        (root / "ignored.txt").write_text("not runtime source\n")
        with mock.patch.object(native_policy, "__file__", str(root / "native_policy.py")):
            first = native_policy._runtime_identity()
            (root / "resources" / "policy.md").write_text("policy two\n")
            second = native_policy._runtime_identity()
            (root / "ignored.txt").write_text("still not source\n")
            third = native_policy._runtime_identity()
        self.assertEqual(3, first["files"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["sha256"], third["sha256"])

    def test_runtime_symlink_or_special_resource_blocks(self):
        root = self.root / "runtime"
        (root / "resources").mkdir(parents=True)
        (root / "module.py").write_text("pass\n")
        link = root / "resources" / "linked"
        link.symlink_to(self.executable)
        with mock.patch.object(native_policy, "__file__", str(root / "native_policy.py")):
            with self.assertRaises(Fault):
                native_policy._runtime_identity()
            link.unlink()
            os.mkfifo(link)
            with self.assertRaises(Fault):
                native_policy._runtime_identity()

    def test_system_tool_version_probe_uses_only_fixed_clean_environment(self):
        def version(argv, **options):
            self.assertEqual([str(self.executable), "--version"], argv)
            self.assertEqual({"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, options["env"])
            options["stdout"].write(b"fixture tool 1.2.3\n")
            return SimpleNamespace(returncode=0)
        with (mock.patch("xiaolvji.sandbox._trusted_tool", return_value=self.executable),
              mock.patch.object(native_policy.subprocess, "run", side_effect=version)):
            result = native_policy._tool_identity("bwrap", "x86_64")
        self.assertEqual("fixture tool 1.2.3", result["version"])

    def test_unsupported_platform_is_rejected_before_boot_id_read(self):
        with (mock.patch.object(native_policy.platform, "system", return_value="Windows"),
              mock.patch.object(native_policy.os, "open") as opened):
            with self.assertRaises(Fault):
                native_policy._machine_identity()
        opened.assert_not_called()

    def test_project_policy_digest_is_stable_for_normal_source_edits(self):
        project = self.root / "project"
        workspace = self.root / "workspace"
        project.mkdir()
        workspace.mkdir()
        (project / "module.py").write_text("first\n")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            first = capture_project_policy(project)
            (project / "module.py").write_text("second\n")
            (project / "new-module.py").write_text("new\n")
            second = capture_project_policy(project, workspace=workspace)
        self.assertEqual(first["policy_digest"], second["policy_digest"])
        self.assertNotEqual(first["checks"][0]["scanned_entries"], second["checks"][0]["scanned_entries"])
        self.assertEqual(["source", "workspace"], [check["role"] for check in second["checks"]])

    def test_project_policy_digest_binds_enrolled_root(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            self.assertNotEqual(capture_project_policy(first)["policy_digest"],
                                capture_project_policy(second)["policy_digest"])

    def test_native_project_policy_presence_blocks_in_any_subdirectory(self):
        project = self.root / "project"
        nested = project / "nested" / "deeper"
        nested.mkdir(parents=True)
        for kind in ("file", "directory", "dangling-symlink"):
            policy = nested / ".codex"
            if kind == "file":
                policy.write_text("PRIVATE_CONFIG_CONTENTS")
            elif kind == "directory":
                policy.mkdir()
            else:
                policy.symlink_to(self.root / "missing")
            with mock.patch.object(native_policy, "_ancestor_policy_absence"):
                with self.subTest(kind=kind), self.assertRaises(Fault) as raised:
                    capture_project_policy(project)
            self.assertNotIn("PRIVATE_CONFIG_CONTENTS", str(raised.exception))
            if kind == "directory":
                policy.rmdir()
            else:
                policy.unlink()

    def test_prepared_workspace_policy_is_checked_separately(self):
        project = self.root / "project"
        workspace = self.root / "workspace"
        project.mkdir()
        workspace.mkdir()
        (workspace / ".codex").mkdir()
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            with self.assertRaises(Fault):
                capture_project_policy(project, workspace=workspace)

    def test_source_and_prepared_native_skill_or_plugin_sources_block_without_reads(self):
        project = self.root / "project"
        workspace = self.root / "workspace"
        project.mkdir()
        workspace.mkdir()
        for root in (project, workspace):
            for location in (root, root / "nested"):
                location.mkdir(exist_ok=True)
                for kind in ("file", "directory", "dangling-symlink"):
                    policy = location / ".agents"
                    if kind == "file":
                        policy.write_text("PRIVATE_SKILL_POLICY")
                    elif kind == "directory":
                        (policy / "skills").mkdir(parents=True)
                        (policy / "skills" / "SKILL.md").write_text("PRIVATE_SKILL_POLICY")
                    else:
                        policy.symlink_to(self.root / "missing")
                    with (mock.patch.object(native_policy, "_ancestor_policy_absence"),
                          mock.patch.object(native_policy, "_regular_bytes") as read):
                        with self.subTest(root=root.name, location=location.name, kind=kind), self.assertRaises(Fault) as raised:
                            capture_project_policy(project, workspace=workspace)
                    self.assertEqual("CAPABILITY_UNSUPPORTED", raised.exception.code)
                    self.assertNotIn("PRIVATE_SKILL_POLICY", str(raised.exception))
                    read.assert_not_called()
                    if kind == "directory":
                        (policy / "skills" / "SKILL.md").unlink()
                        (policy / "skills").rmdir()
                        policy.rmdir()
                    else:
                        policy.unlink()

    def test_ancestor_native_skill_and_plugin_sources_block_including_aliases(self):
        project = self.root / "project"
        project.mkdir()
        policy = self.root / ".agents"
        for kind in ("file", "directory", "dangling-symlink"):
            if kind == "file":
                policy.write_text("PRIVATE_ANCESTOR_SKILL")
            elif kind == "directory":
                policy.mkdir()
            else:
                policy.symlink_to(self.root / "missing")
            with mock.patch.object(native_policy, "_regular_bytes") as read:
                with self.subTest(kind=kind), self.assertRaises(Fault) as raised:
                    capture_project_policy(project)
            self.assertEqual("CAPABILITY_UNSUPPORTED", raised.exception.code)
            self.assertNotIn("PRIVATE_ANCESTOR_SKILL", str(raised.exception))
            read.assert_not_called()
            if kind == "directory":
                policy.rmdir()
            else:
                policy.unlink()

    def test_root_hook_policy_and_instruction_symlinks_block(self):
        project = self.root / "project"
        project.mkdir()
        hooks = project / "hooks.json"
        hooks.write_text("PRIVATE_HOOK_CONTENTS")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            with self.assertRaises(Fault):
                capture_project_policy(project)
            hooks.unlink()
            (project / "AGENTS.md").symlink_to(self.root / "external-instructions")
            with self.assertRaises(Fault):
                capture_project_policy(project)

    def test_in_tree_regular_instructions_are_hashed_without_returning_contents(self):
        project = self.root / "project"
        nested = project / "nested"
        nested.mkdir(parents=True)
        (project / "AGENTS.md").write_text("PRIVATE_INSTRUCTIONS")
        (nested / "AGENTS.override.md").write_text("PRIVATE_NESTED_INSTRUCTIONS")
        with (mock.patch.object(native_policy, "_ancestor_policy_absence"),
              mock.patch.object(Path, "read_text") as text,
              mock.patch.object(Path, "read_bytes") as data,
              mock.patch.object(native_policy, "_regular_bytes", wraps=native_policy._regular_bytes) as bounded_read):
            result = capture_project_policy(project)
        self.assertEqual("standalone-project-v1", result["policy_profile"])
        text.assert_not_called()
        data.assert_not_called()
        self.assertEqual(2, bounded_read.call_count)
        self.assertEqual({"AGENTS.md", "nested/AGENTS.override.md"}, set(result["instructions"]))
        self.assertEqual("sha256:" + hashlib.sha256(b"PRIVATE_INSTRUCTIONS").hexdigest(),
                         result["instructions"]["AGENTS.md"]["sha256"])
        self.assertNotIn("PRIVATE_INSTRUCTIONS", json.dumps(result))

    def test_prepared_workspace_preserves_exact_instruction_paths_and_bytes(self):
        project = self.root / "project"
        workspace = self.root / "workspace"
        instruction = b"\xef\xbb\xbfKeep original bytes.\r\n"
        for root in (project, workspace):
            (root / "module").mkdir(parents=True)
            (root / "AGENTS.md").write_bytes(instruction)
            (root / "module" / "AGENTS.override.md").write_bytes(b"Module instructions\n")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            source = capture_project_policy(project)
            prepared = capture_project_policy(project, workspace=workspace)
            (project / "module" / "code.py").write_text("changed ordinary source\n")
            changed_code = capture_project_policy(project, workspace=workspace)
        self.assertEqual(source["policy_digest"], prepared["policy_digest"])
        self.assertEqual(prepared["policy_digest"], changed_code["policy_digest"])
        self.assertEqual(2, prepared["checks"][1]["instruction_count"])

    def test_instruction_changes_invalidate_policy_identity(self):
        project = self.root / "project"
        project.mkdir()
        instruction = project / "AGENTS.md"
        instruction.write_bytes(b"policy one\n")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            first = capture_project_policy(project)
            instruction.write_bytes(b"policy two\n")
            second = capture_project_policy(project)
            instruction.rename(project / "AGENTS.override.md")
            renamed = capture_project_policy(project)
        self.assertNotEqual(first["instruction_digest"], second["instruction_digest"])
        self.assertNotEqual(first["policy_digest"], second["policy_digest"])
        self.assertNotEqual(second["policy_digest"], renamed["policy_digest"])

    def test_dirty_untracked_or_dependency_instruction_mismatch_blocks(self):
        project = self.root / "project"
        workspace = self.root / "workspace"
        project.mkdir()
        workspace.mkdir()
        source = project / "AGENTS.md"
        prepared = workspace / "AGENTS.md"
        for kind in ("dirty", "untracked-source", "dependency-added"):
            source.write_bytes(b"original policy\n")
            prepared.write_bytes(b"original policy\n")
            if kind == "dirty":
                source.write_bytes(b"changed source policy\n")
            elif kind == "untracked-source":
                prepared.unlink()
            else:
                source.unlink()
            with mock.patch.object(native_policy, "_ancestor_policy_absence"):
                with self.subTest(kind=kind), self.assertRaises(Fault) as raised:
                    capture_project_policy(project, workspace=workspace)
            self.assertEqual("NATIVE_PROJECT_POLICY_UNSUPPORTED", raised.exception.code)
            if source.exists():
                source.unlink()
            if prepared.exists():
                prepared.unlink()

    def test_instruction_capture_has_per_file_and_aggregate_budgets(self):
        project = self.root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_bytes(b"x" * (native_policy.MAX_INSTRUCTION_BYTES + 1))
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            with self.assertRaises(Fault):
                capture_project_policy(project)
            (project / "AGENTS.md").write_bytes(b"aa")
            (project / "AGENTS.override.md").write_bytes(b"bb")
            with mock.patch.object(native_policy, "MAX_PROJECT_INSTRUCTION_BYTES", 3):
                with self.assertRaises(Fault) as raised:
                    capture_project_policy(project)
            self.assertIn("aggregate", str(raised.exception))

    def test_instruction_directory_and_fifo_cannot_be_accepted_as_empty_policy(self):
        project = self.root / "project"
        project.mkdir()
        instruction = project / "AGENTS.md"
        instruction.mkdir()
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            with self.assertRaises(Fault):
                capture_project_policy(project)
            instruction.rmdir()
            os.mkfifo(instruction)
            with self.assertRaises(Fault):
                capture_project_policy(project)

    def test_empty_instruction_files_cannot_exceed_index_budget(self):
        project = self.root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_bytes(b"")
        (project / "AGENTS.override.md").write_bytes(b"")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            for setting, maximum in (("MAX_PROJECT_INSTRUCTION_FILES", 1), ("MAX_INSTRUCTION_INDEX_BYTES", 1)):
                with mock.patch.object(native_policy, setting, maximum):
                    with self.subTest(setting=setting), self.assertRaises(Fault) as raised:
                        capture_project_policy(project)
                    self.assertIn("index", str(raised.exception))

    def test_git_metadata_and_arbitrary_symlink_targets_are_not_scanned(self):
        project = self.root / "project"
        (project / ".git" / ".codex").mkdir(parents=True)
        external = self.root / "outside"
        (external / ".codex").mkdir(parents=True)
        (project / "alias").symlink_to(external, target_is_directory=True)
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            result = capture_project_policy(project)
        self.assertEqual(1, result["checks"][0]["scanned_entries"])

    def test_ancestor_instructions_and_native_policy_block(self):
        project = self.root / "project"
        project.mkdir()
        for name in (".codex", "hooks.json", "AGENTS.md", "AGENTS.override.md"):
            marker = self.root / name
            marker.write_text("PRIVATE_ANCESTOR_POLICY")
            with self.subTest(name=name), self.assertRaises(Fault) as raised:
                capture_project_policy(project)
            self.assertNotIn("PRIVATE_ANCESTOR_POLICY", str(raised.exception))
            marker.unlink()

    def test_project_inspection_rejects_root_alias_and_non_directory(self):
        project = self.root / "project"
        project.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(project, target_is_directory=True)
        file = self.root / "file"
        file.write_text("data")
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            for path in (alias, file, self.root / "missing", Path("relative")):
                with self.subTest(path=str(path)), self.assertRaises(Fault):
                    capture_project_policy(path)

    def test_project_walk_is_bounded_by_entries_and_depth(self):
        project = self.root / "project"
        (project / "a" / "b").mkdir(parents=True)
        with mock.patch.object(native_policy, "_ancestor_policy_absence"):
            with mock.patch.object(native_policy, "MAX_PROJECT_POLICY_ENTRIES", 1):
                with self.assertRaises(Fault) as raised:
                    capture_project_policy(project)
                self.assertIn("entry-count", str(raised.exception))
            with mock.patch.object(native_policy, "MAX_PROJECT_POLICY_DEPTH", 1):
                with self.assertRaises(Fault) as raised:
                    capture_project_policy(project)
                self.assertIn("directory-depth", str(raised.exception))

    def test_project_directory_mutation_during_walk_blocks(self):
        project = self.root / "project"
        project.mkdir()
        before = project.stat()
        after = SimpleNamespace(**{key: getattr(before, key) for key in (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")})
        after.st_mtime_ns += 1
        with (mock.patch.object(native_policy, "_ancestor_policy_absence"),
              mock.patch.object(native_policy.os, "fstat", side_effect=[before, before, after])):
            with self.assertRaises(Fault) as raised:
                capture_project_policy(project)
        self.assertIn("changed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
