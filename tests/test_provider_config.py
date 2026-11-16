"""Explicit provider validation and synthetic private credential-file boundaries."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, canonical_json, digest
from xiaolvji.provider_config import (
    load_provider_configuration, normalize_provider_configuration,
    read_explicit_credentials,
)


SYNTHETIC_SECRET = "synthetic-provider-key-never-use-upstream"


def configuration_value(credential_path="/operator-only/fixture-token"):
    host = {"schema_version": 1, "engine": "codex", "provider_id": "fixture",
            "api": "openai-responses", "model": "owner-exact-model",
            "quality": {"model_reasoning_effort": "ultra", "service_tier": "fast"}}
    return {"schema_version": 1, "global_request_limit": 2,
            "providers": [{"profile_id": "fixture", "api": "openai-responses",
                           "upstream_url": "https://fixture.invalid/exact/responses",
                           "model": "owner-exact-model",
                           "request_constraints": {"reasoning": {"effort": "ultra"}, "service_tier": "priority"}}],
            "bindings": [{"project_id": "project", "host": host}],
            "credential_files": {"fixture": str(credential_path)}}


class ProviderConfigurationTests(unittest.TestCase):
    def test_private_credentials_cannot_be_inside_implicit_runtime_mounts(self):
        from xiaolvji.sandbox import readable_host_sources
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "python-runtime"
            runtime.mkdir()
            token = runtime / "private-provider-token"
            token.write_text(SYNTHETIC_SECRET)
            token.chmod(0o600)
            config = normalize_provider_configuration(configuration_value(token))
            with patch("xiaolvji.sandbox.readable_host_sources", return_value=(runtime,)), \
                    patch("xiaolvji.provider_config.os.open", side_effect=AssertionError("visible secret must never be opened")):
                with self.assertRaises(Fault) as caught:
                    read_explicit_credentials(config)
            self.assertEqual("PROVIDER_CREDENTIAL_UNAVAILABLE", caught.exception.code)
            with patch("xiaolvji.sandbox.readable_host_sources", return_value=(token,)):
                with self.assertRaises(Fault):
                    read_explicit_credentials(config)
            # The production inventory comes from sandbox construction, not a
            # separate hardcoded provider-specific approximation.
            self.assertTrue(readable_host_sources())

    def test_configuration_and_views_are_detached_immutable_snapshots(self):
        supplied = configuration_value()
        config = normalize_provider_configuration(supplied)
        original_digest = config.digest
        supplied["bindings"][0]["host"]["model"] = "changed"
        supplied["providers"][0]["request_constraints"]["reasoning"]["effort"] = "low"
        value = config.value
        value["bindings"][0]["host"]["quality"]["model_reasoning_effort"] = "low"
        host = config.host("project", "codex")
        host["model"] = "changed"
        profiles = config.profiles()
        profiles["fixture"].request_constraints["reasoning"]["effort"] = "low"
        self.assertEqual("owner-exact-model", config.host("project", "codex")["model"])
        self.assertEqual("ultra", config.host("project", "codex")["quality"]["model_reasoning_effort"])
        self.assertEqual("ultra", config.profiles()["fixture"].to_dict()["request_constraints"]["reasoning"]["effort"])
        self.assertEqual(original_digest, config.digest)
        self.assertEqual(digest(config.value), config.digest)

    def test_validation_and_summary_do_not_read_credential_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            supplied = configuration_value(root / "does-not-exist-token")
            path.write_text(canonical_json(supplied), encoding="utf-8")
            with patch("xiaolvji.provider_config.os.open", side_effect=AssertionError("credential open during validation")), \
                    patch("xiaolvji.provider_config.read_explicit_credentials", side_effect=AssertionError("credential read during validation")):
                config = load_provider_configuration(path)
                summary = config.summary()
            self.assertFalse(summary["credentials_read"])
            self.assertFalse(summary["native_host_execution_verified"])
            self.assertNotIn(str(root), json.dumps(summary))
            self.assertNotIn("upstream_url", json.dumps(summary))
            self.assertNotIn("credential_files", json.dumps(summary))
            self.assertEqual(config.digest, summary["configuration_digest"])

    def test_unknown_fields_and_duplicate_provider_or_binding_rejected(self):
        cases = []
        for key in ("api_key", "headers", "fallback_model", "env", "extra"):
            value = configuration_value()
            value[key] = SYNTHETIC_SECRET
            cases.append(value)
        value = configuration_value()
        value["providers"].append(copy.deepcopy(value["providers"][0]))
        cases.append(value)
        value = configuration_value()
        value["bindings"].append(copy.deepcopy(value["bindings"][0]))
        cases.append(value)
        value = configuration_value()
        value["bindings"][0]["extra"] = True
        cases.append(value)
        value = configuration_value()
        value["providers"][0]["headers"] = {"Authorization": SYNTHETIC_SECRET}
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(Fault) as caught:
                normalize_provider_configuration(value)
            self.assertNotIn(SYNTHETIC_SECRET, json.dumps(caught.exception.as_dict()))

    def test_duplicate_json_keys_fail_before_configuration_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            raw = canonical_json(configuration_value())
            path.write_text('{"schema_version":1,' + raw[1:], encoding="utf-8")
            with self.assertRaises(Fault) as caught:
                load_provider_configuration(path)
            self.assertIn("Duplicate JSON key", caught.exception.message)

    def test_closed_shape_limits_paths_and_unknown_host_fail(self):
        cases = []
        for key, invalid in (("schema_version", True), ("schema_version", 2),
                             ("global_request_limit", True), ("global_request_limit", 0),
                             ("global_request_limit", 65), ("providers", []), ("bindings", []),
                             ("credential_files", {})):
            value = configuration_value()
            value[key] = invalid
            cases.append(value)
        for invalid in ("relative/key", "/a/../key", "/a/./key", "/a//key", "/a/key/", "/a/\x00key"):
            value = configuration_value(invalid)
            cases.append(value)
        value = configuration_value()
        value["bindings"][0]["host"]["provider_id"] = "not-registered"
        cases.append(value)
        value = configuration_value()
        value["credential_files"]["other"] = "/operator-only/other-token"
        cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(Fault):
                normalize_provider_configuration(value)
        config = normalize_provider_configuration(configuration_value())
        for project, engine in (("missing", "codex"), ("project", "zcode")):
            with self.subTest(project=project, engine=engine), self.assertRaises(Fault) as caught:
                config.host(project, engine)
            self.assertEqual("HOST_NOT_CONFIGURED", caught.exception.code)

    def test_exact_model_api_and_native_quality_must_agree(self):
        cases = []
        for key, value in (("model", "lower-model"), ("api", "openai-chat-completions"),
                           ("request_constraints", {"reasoning": {"effort": "high"}, "service_tier": "priority"}),
                           ("request_constraints", {"reasoning": {"effort": "ultra"}}),
                           ("request_constraints", {"reasoning": {"effort": "ultra"}, "service_tier": "fast"}),
                           ("request_constraints", {"reasoning": {"effort": "ultra"}, "service_tier": "priority", "temperature": 0.1})):
            config = configuration_value()
            config["providers"][0][key] = value
            if key == "api":
                config["providers"][0]["request_constraints"] = {"reasoning_effort": "ultra", "service_tier": "priority"}
            cases.append(config)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(Fault) as caught:
                normalize_provider_configuration(value)
            self.assertEqual("PROVIDER_QUALITY_MISMATCH", caught.exception.code)

    def test_quality_comparison_distinguishes_integer_float_and_boolean(self):
        value = configuration_value()
        value["bindings"][0]["host"] = {
            "schema_version": 1, "engine": "zcode", "provider_id": "fixture",
            "api": "openai-responses", "model": "owner-exact-model",
            "quality": {"reasoningLevel": "ultra", "maxOutputTokens": 1},
            "zcode_model_properties": {"contextWindow": 32_000, "requiresMfjsToolSchema": False,
                "inputFormat": {"supportsText": True, "supportsImage": False, "supportsVideo": False,
                                "supportsAudio": False, "supportsPdf": False},
                "outputFormat": {"supportsText": True}, "supportsToolCall": True,
                "supportsJsonSchemaOutput": False, "supportsNativeWebSearch": False,
                "supportsMidConversationSystem": False},
        }
        value["providers"][0]["request_constraints"] = {"reasoning": {"effort": "ultra"}, "max_output_tokens": 1}
        normalize_provider_configuration(value)
        for numeric_alias in (True, 1.0):
            altered = copy.deepcopy(value)
            altered["providers"][0]["request_constraints"]["max_output_tokens"] = numeric_alias
            with self.subTest(value=numeric_alias), self.assertRaises(Fault) as caught:
                normalize_provider_configuration(altered)
            self.assertEqual("PROVIDER_QUALITY_MISMATCH", caught.exception.code)


class ExplicitCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-credential-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def create_key(self, name="operator.key", content=None, mode=0o600):
        path = self.root / name
        path.write_bytes((SYNTHETIC_SECRET + "\n").encode() if content is None else content)
        path.chmod(mode)
        return path

    def read(self, path, forbidden=()):
        config = normalize_provider_configuration(configuration_value(path))
        return read_explicit_credentials(config, forbidden_roots=forbidden)

    def assert_unavailable(self, path, forbidden=()):
        with self.assertRaises(Fault) as caught:
            self.read(path, forbidden)
        self.assertEqual("PROVIDER_CREDENTIAL_UNAVAILABLE", caught.exception.code)
        encoded = json.dumps(caught.exception.as_dict())
        self.assertNotIn(str(path), encoded)
        self.assertNotIn(SYNTHETIC_SECRET, encoded)

    def test_only_explicit_private_synthetic_file_is_read(self):
        key = self.create_key()
        self.assertEqual({"fixture": SYNTHETIC_SECRET}, self.read(key))
        key.write_bytes(SYNTHETIC_SECRET.encode())
        self.assertEqual({"fixture": SYNTHETIC_SECRET}, self.read(key))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-credential-must-not-be-used",
                                     "ZCODE_API_KEY": "ambient-other-credential"}):
            self.assert_unavailable(self.root / "missing.key")

    def test_symlink_leaf_and_parent_are_rejected(self):
        key = self.create_key()
        leaf = self.root / "linked.key"
        leaf.symlink_to(key)
        self.assert_unavailable(leaf)
        link = self.root / "linked-parent"
        actual = self.root / "actual-parent"
        actual.mkdir()
        nested = actual / "key"
        nested.write_bytes(SYNTHETIC_SECRET.encode())
        nested.chmod(0o600)
        link.symlink_to(actual, target_is_directory=True)
        self.assert_unavailable(link / "key")

    def test_hardlinks_unsafe_permissions_and_nonregular_files_rejected(self):
        key = self.create_key()
        hardlink = self.root / "hardlink.key"
        os.link(key, hardlink)
        self.assert_unavailable(key)
        self.assert_unavailable(hardlink)
        for mode in (0o640, 0o604, 0o644, 0o666):
            path = self.create_key("mode-" + str(mode), mode=mode)
            self.assert_unavailable(path)
        directory = self.root / "directory"
        directory.mkdir(mode=0o700)
        self.assert_unavailable(directory)
        fifo = self.root / "pipe"
        os.mkfifo(fifo, mode=0o600)
        self.assert_unavailable(fifo)

    def test_source_and_workspace_credential_locations_rejected(self):
        key = self.create_key("inside-source.key")
        self.assert_unavailable(key, forbidden=(self.root,))
        public_alias = self.root / "source-alias"
        public_alias.symlink_to(self.root, target_is_directory=True)
        self.assert_unavailable(key, forbidden=(public_alias,))

    def test_size_ascii_whitespace_and_line_endings_fail_closed(self):
        for index, content in enumerate((b"short", b"", b"x" * 8194, b" " + b"x" * 32,
                                         b"x" * 32 + b"\n\n", b"x" * 32 + b"\r\n",
                                         b"x" * 32 + b"\x00", b"x" * 32 + b"\xff")):
            with self.subTest(index=index):
                self.assert_unavailable(self.create_key("invalid-" + str(index), content))
        maximum = self.create_key("maximum", b"x" * 8192 + b"\n")
        self.assertEqual("x" * 8192, self.read(maximum)["fixture"])

    def test_failure_does_not_return_previously_read_credentials(self):
        first = self.create_key()
        value = configuration_value(first)
        other = copy.deepcopy(value["providers"][0])
        other["profile_id"] = "second"
        value["providers"].append(other)
        value["credential_files"]["second"] = str(self.root / "missing-second.key")
        config = normalize_provider_configuration(value)
        with self.assertRaises(Fault) as caught:
            read_explicit_credentials(config)
        self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception.as_dict()))


if __name__ == "__main__":
    unittest.main()
