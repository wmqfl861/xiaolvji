from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import unittest

from xiaolvji.contracts import Fault, digest
from xiaolvji.host_config import (
    BRIDGE_PROVIDER_ID, CAPABILITY_ENV, CODEX_CONFIG_PATH, CODEX_CATALOG_PATH, PRIVATE_HOME,
    ZCODE_BUILTIN_PATH, ZCODE_PERSONAL_PATH, derive_request_constraints,
    normalize_host_snapshot, render_host_config,
)


CAPABILITY = "synthetic_attempt_capability_" + "A" * 32
BRIDGE = "http://127.0.0.1:43121"


def codex_snapshot():
    return {"schema_version": 1, "engine": "codex", "provider_id": "registered-provider",
            "api": "openai-responses", "model": "owner/exact-model:revision",
            "quality": {"model_reasoning_effort": "ultra"}}


def zcode_snapshot(api="openai-responses"):
    quality = {"reasoningLevel": "high", "maxOutputTokens": 32_000}
    if api == "anthropic-messages":
        quality["thinking"] = {"type": "enabled", "budget_tokens": 8_000}
    return {"schema_version": 1, "engine": "zcode", "provider_id": "registered-provider",
            "api": api, "model": "owner/exact-model:revision", "quality": quality,
            "zcode_model_properties": {
                "contextWindow": 200_000, "requiresMfjsToolSchema": False,
                "inputFormat": {"supportsText": False, "supportsImage": False,
                                "supportsVideo": False, "supportsAudio": False, "supportsPdf": False},
                "outputFormat": {"supportsText": False}, "supportsToolCall": False,
                "supportsJsonSchemaOutput": False, "supportsNativeWebSearch": False,
                "supportsMidConversationSystem": False,
            }}


def codex_catalog_snapshot():
    snapshot = codex_snapshot()
    snapshot["codex_model_catalog"] = {"models": [{
        "slug": snapshot["model"], "display_name": "Synthetic exact model metadata",
        "base_instructions": "Follow the approved task and capability boundaries.",
        "supported_reasoning_levels": [{"effort": "ultra", "description": "Explicit synthetic effort"}],
        "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
        "priority": 0, "support_verbosity": True, "context_window": 200000,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [], "upgrade": None,
        "default_reasoning_level": "ultra", "default_reasoning_summary": "detailed",
    }]}
    return snapshot


def files_by_path(rendered):
    return {entry["relative_path"]: entry for entry in rendered["files"]}


class HostConfigTests(unittest.TestCase):
    def test_snapshot_is_explicit_detached_and_does_not_insert_defaults(self):
        original = codex_snapshot()
        normalized = normalize_host_snapshot(original)
        self.assertEqual(original, normalized)
        original["quality"]["model_reasoning_effort"] = "low"
        self.assertEqual("ultra", normalized["quality"]["model_reasoning_effort"])
        self.assertEqual({"model_reasoning_effort"}, set(normalized["quality"]))

    def test_exact_codex_model_and_all_supported_quality_are_preserved(self):
        snapshot = codex_snapshot()
        snapshot["quality"].update(model_reasoning_summary="detailed", model_verbosity="high",
                                   service_tier="fast", model_context_window=200_000,
                                   model_auto_compact_token_limit=155_000)
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        config = tomllib.loads(files_by_path(rendered)[CODEX_CONFIG_PATH]["content"])
        self.assertEqual(snapshot["model"], config["model"])
        self.assertEqual(BRIDGE_PROVIDER_ID, config["model_provider"])
        for key, value in snapshot["quality"].items():
            self.assertEqual(value, config[key])
        self.assertEqual({"model": snapshot["model"], "reasoning": {"effort": "ultra", "summary": "detailed"},
                          "text": {"verbosity": "high"}, "service_tier": "priority"},
                         derive_request_constraints(snapshot))
        self.assertNotIn("max_output_tokens", rendered["request_constraints"])
        self.assertNotIn("model_context_window", rendered["request_constraints"])
        self.assertFalse(rendered["evidence"]["runtime_verified"])
        self.assertFalse(rendered["evidence"]["real_model_request_tested"])

    def test_codex_transport_is_only_private_bridge_and_capability_env(self):
        rendered = render_host_config(codex_snapshot(), BRIDGE, CAPABILITY)
        config = tomllib.loads(files_by_path(rendered)[CODEX_CONFIG_PATH]["content"])
        self.assertEqual({"name": "Xiaolvji broker bridge", "base_url": BRIDGE + "/v1",
                          "env_key": CAPABILITY_ENV, "wire_api": "responses",
                          "requires_openai_auth": False, "supports_websockets": False},
                         config["model_providers"][BRIDGE_PROVIDER_ID])
        self.assertEqual({"CODEX_HOME": PRIVATE_HOME + "/.codex", CAPABILITY_ENV: CAPABILITY}, rendered["env"])
        self.assertEqual({CODEX_CONFIG_PATH}, set(files_by_path(rendered)))
        self.assertNotIn(CAPABILITY, json.dumps(rendered["files"]))
        self.assertNotIn(CAPABILITY, json.dumps(rendered["argv_extra"]))
        self.assertEqual(0o600, rendered["files"][0]["mode"])

    def test_codex_cli_pins_exact_model_quality_transport_and_no_remote_search(self):
        snapshot = codex_snapshot()
        snapshot["quality"]["service_tier"] = "priority"
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        argv = rendered["argv_extra"]
        self.assertTrue(all(flag == "-c" for flag in argv[::2]))
        keys = {item.split("=", 1)[0] for item in argv[1::2]}
        self.assertEqual({"model", "model_provider", "model_reasoning_effort", "service_tier", "web_search",
                          *("model_providers." + BRIDGE_PROVIDER_ID + "." + key for key in
                            ("name", "base_url", "env_key", "wire_api", "requires_openai_auth", "supports_websockets"))}, keys)
        for forbidden in ("approval_policy", "sandbox_mode", "trust_level", "features", "hooks", "rules"):
            self.assertFalse(any(key == forbidden or key.startswith(forbidden + ".") for key in keys))
        self.assertEqual("priority", rendered["request_constraints"]["service_tier"])
        config = tomllib.loads(files_by_path(rendered)[CODEX_CONFIG_PATH]["content"])
        self.assertEqual("disabled", config["web_search"])
        self.assertIn('web_search="disabled"', argv)

    def test_explicit_codex_catalog_is_preserved_and_pinned_read_only(self):
        snapshot = codex_catalog_snapshot()
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        files = files_by_path(rendered)
        self.assertEqual(snapshot["codex_model_catalog"], json.loads(files[CODEX_CATALOG_PATH]["content"]))
        self.assertEqual(0o600, files[CODEX_CATALOG_PATH]["mode"])
        config = tomllib.loads(files[CODEX_CONFIG_PATH]["content"])
        self.assertEqual(PRIVATE_HOME + "/" + CODEX_CATALOG_PATH, config["model_catalog_json"])
        self.assertIn('model_catalog_json="' + PRIVATE_HOME + '/' + CODEX_CATALOG_PATH + '"', rendered["argv_extra"])
        detached = normalize_host_snapshot(snapshot)
        snapshot["codex_model_catalog"]["models"][0]["base_instructions"] = "changed"
        self.assertNotEqual(snapshot["codex_model_catalog"], detached["codex_model_catalog"])
        self.assertEqual({"model": snapshot["model"], "reasoning": {"effort": "ultra"}}, rendered["request_constraints"])

    def test_catalog_exact_match_reasoning_and_context_cannot_fall_back(self):
        cases = []
        for key in ("slug", "base_instructions", "context_window", "supported_reasoning_levels", "shell_type"):
            value = codex_catalog_snapshot()
            del value["codex_model_catalog"]["models"][0][key]
            cases.append(value)
        for key, item in (("slug", "different-model"), ("context_window", True),
                          ("supported_reasoning_levels", [{"effort": "high", "description": "lower"}]),
                          ("supported_in_api", False), ("truncation_policy", {"mode": [], "limit": 10000})):
            value = codex_catalog_snapshot()
            value["codex_model_catalog"]["models"][0][key] = item
            cases.append(value)
        value = codex_catalog_snapshot()
        value["codex_model_catalog"]["models"] *= 2
        cases.append(value)
        value = codex_catalog_snapshot()
        value["quality"]["model_verbosity"] = "high"
        value["codex_model_catalog"]["models"][0]["support_verbosity"] = False
        cases.append(value)
        value = zcode_snapshot()
        value["codex_model_catalog"] = codex_catalog_snapshot()["codex_model_catalog"]
        cases.append(value)
        for value in cases:
            with self.subTest(catalog=value.get("engine")), self.assertRaises(Fault):
                normalize_host_snapshot(value)

    def test_catalog_size_and_unique_effort_declarations_are_bounded(self):
        for mutation in ("size", "duplicate_effort", "nonstring_effort"):
            value = codex_catalog_snapshot()
            entry = value["codex_model_catalog"]["models"][0]
            if mutation == "size":
                entry["base_instructions"] = "x" * (1024 * 1024)
            elif mutation == "duplicate_effort":
                entry["supported_reasoning_levels"] *= 2
            else:
                entry["supported_reasoning_levels"][0]["effort"] = False
            with self.subTest(mutation=mutation), self.assertRaises(Fault):
                normalize_host_snapshot(value)

    def test_zcode_has_exact_personal_selection_and_pinned_builtin(self):
        snapshot = zcode_snapshot()
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        files = files_by_path(rendered)
        self.assertEqual({ZCODE_PERSONAL_PATH, ZCODE_BUILTIN_PATH}, set(files))
        personal = json.loads(files[ZCODE_PERSONAL_PATH]["content"])["config"]
        rule = personal["providerConfigRules"]["providerRules"][0]
        self.assertEqual(BRIDGE_PROVIDER_ID, rule["providerId"])
        self.assertEqual({"type": "api-key", "apiKey": CAPABILITY}, rule["config"]["access"])
        self.assertEqual({"type": "openai-responses", "baseUrl": BRIDGE + "/v1"}, rule["config"]["api"])
        self.assertEqual([snapshot["model"]], rule["config"]["personalModelIds"])
        self.assertEqual({"providerId": BRIDGE_PROVIDER_ID, "modelId": snapshot["model"],
                          "options": snapshot["quality"]}, personal["defaultModelSelection"])
        self.assertEqual({"ZCODE_DATA_BASE_DIR": PRIVATE_HOME,
                          "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": PRIVATE_HOME + "/" + ZCODE_PERSONAL_PATH,
                          "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": PRIVATE_HOME + "/" + ZCODE_BUILTIN_PATH}, rendered["env"])
        self.assertEqual([], rendered["argv_extra"])
        self.assertEqual("source_verified_execution_unverified", rendered["evidence"]["level"])
        self.assertTrue(all(item["mode"] == 0o600 for item in files.values()))

    def test_every_zcode_capability_boolean_is_preserved_including_false(self):
        for boolean in (False, True):
            snapshot = zcode_snapshot()
            props = snapshot["zcode_model_properties"]
            for key in props:
                if isinstance(props[key], dict):
                    props[key] = {name: boolean for name in props[key]}
                elif type(props[key]) is bool:
                    props[key] = boolean
            rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
            builtin = json.loads(files_by_path(rendered)[ZCODE_BUILTIN_PATH]["content"])
            rule = builtin["config"]["modelConfigRules"]["modelRules"][0]
            self.assertEqual(props, rule["config"]["properties"])
            self.assertEqual(snapshot["quality"]["maxOutputTokens"], rule["config"]["optionSpecs"]["maxOutputTokens"]["max"])
            self.assertEqual(["high"], rule["config"]["optionSpecs"]["reasoningLevel"]["values"])

    def test_zcode_model_match_is_exact_and_escaped(self):
        snapshot = zcode_snapshot()
        snapshot["model"] = "org/model.v1+alpha"
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        builtin = json.loads(files_by_path(rendered)[ZCODE_BUILTIN_PATH]["content"])
        pattern = builtin["config"]["modelConfigRules"]["modelRules"][0]["modelMatch"]
        self.assertEqual(r"^org/model\.v1\+alpha$", pattern)
        self.assertIsNotNone(re.fullmatch(pattern, snapshot["model"]))
        self.assertIsNone(re.fullmatch(pattern, "org/modelXv11alpha"))

    def test_all_three_zcode_wire_protocols_map_quality_exactly(self):
        expected = {
            "openai-responses": {"reasoning": {"effort": "high"}, "max_output_tokens": 32_000},
            "openai-chat-completions": {"reasoning_effort": "high", "max_completion_tokens": 32_000},
            "anthropic-messages": {"thinking": {"type": "enabled", "budget_tokens": 8_000}, "max_tokens": 32_000},
        }
        for api, fields in expected.items():
            with self.subTest(api=api):
                snapshot = zcode_snapshot(api)
                rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
                self.assertEqual({"model": snapshot["model"], **fields}, rendered["request_constraints"])
                builtin = json.loads(files_by_path(rendered)[ZCODE_BUILTIN_PATH]["content"])
                specs = builtin["config"]["modelConfigRules"]["modelRules"][0]["config"]["optionSpecs"]
                self.assertEqual(32_000, specs["maxOutputTokens"]["max"])
                self.assertTrue(specs["maxOutputTokens"]["map"].endswith(":maxOutputTokens}"))
                if api == "anthropic-messages":
                    self.assertEqual({"thinking": fields["thinking"]}, json.loads(specs["reasoningLevel"]["map"]))

    def test_anthropic_adaptive_and_output_effort_are_not_discarded(self):
        snapshot = zcode_snapshot("anthropic-messages")
        snapshot["quality"].update(thinking={"type": "adaptive"}, output_config={"effort": "max"})
        constraints = derive_request_constraints(snapshot)
        self.assertEqual({"type": "adaptive"}, constraints["thinking"])
        self.assertEqual({"effort": "max"}, constraints["output_config"])
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        builtin = json.loads(files_by_path(rendered)[ZCODE_BUILTIN_PATH]["content"])
        mapping = builtin["config"]["modelConfigRules"]["modelRules"][0]["config"]["optionSpecs"]["reasoningLevel"]["map"]
        self.assertEqual({"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}, json.loads(mapping))

    def test_missing_exact_model_quality_or_properties_blocks(self):
        cases = []
        for key in codex_snapshot():
            case = codex_snapshot()
            del case[key]
            cases.append(case)
        case = codex_snapshot()
        case["quality"] = {}
        cases.append(case)
        case = zcode_snapshot()
        del case["zcode_model_properties"]
        cases.append(case)
        for key in zcode_snapshot()["zcode_model_properties"]:
            case = zcode_snapshot()
            del case["zcode_model_properties"][key]
            cases.append(case)
        for key in ("reasoningLevel", "maxOutputTokens", "thinking"):
            case = zcode_snapshot("anthropic-messages")
            del case["quality"][key]
            cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Fault):
                normalize_host_snapshot(case)

    def test_unknown_settings_and_credential_fields_never_disappear(self):
        cases = []
        for key in ("api_key", "auth", "headers", "env", "base_url", "approval_policy", "raw_config"):
            case = codex_snapshot()
            case[key] = "UPSTREAM_SECRET_MUST_NOT_APPEAR_IN_FAULT"
            cases.append(case)
        for key in ("model_max_output_tokens", "temperature", "fallback_model", "agents.enabled"):
            case = codex_snapshot()
            case["quality"][key] = 1
            cases.append(case)
        case = zcode_snapshot()
        case["quality"]["model_reasoning_effort"] = "high"
        cases.append(case)
        case = zcode_snapshot()
        case["zcode_model_properties"]["futureCapability"] = True
        cases.append(case)
        case = zcode_snapshot("anthropic-messages")
        case["quality"]["thinking"]["unknown"] = True
        cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Fault) as caught:
                normalize_host_snapshot(case)
            self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)
            self.assertNotIn("UPSTREAM_SECRET", json.dumps(caught.exception.as_dict()))

    def test_unsupported_or_ambiguous_values_block_without_normalization(self):
        cases = []
        for key, value in (("engine", "other"), ("schema_version", True), ("schema_version", 2),
                           ("api", "openai-chat-completions"), ("provider_id", "../provider"),
                           ("model", " model"), ("model", 'm"\n[auth]'), ("model", "m\x00x")):
            case = codex_snapshot()
            case[key] = value
            cases.append(case)
        for value in (True, None, " high", "high\n", "", ["high"]):
            case = codex_snapshot()
            case["quality"]["model_reasoning_effort"] = value
            cases.append(case)
        for value in ("none", "verbose", False, {}):
            case = codex_snapshot()
            case["quality"]["model_reasoning_summary"] = value
            cases.append(case)
        for value in (False, 0, -1, 1.5, 2**31, float("nan")):
            case = zcode_snapshot()
            case["quality"]["maxOutputTokens"] = value
            cases.append(case)
        case = zcode_snapshot()
        case["zcode_model_properties"]["inputFormat"]["supportsPdf"] = 0
        cases.append(case)
        case = zcode_snapshot("anthropic-messages")
        case["quality"]["thinking"]["type"] = "adaptive"
        cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Fault):
                normalize_host_snapshot(case)

    def test_bridge_origin_cannot_select_network_host_path_or_credentials(self):
        for url in ("https://127.0.0.1:43121", "http://localhost:43121", "http://0.0.0.0:43121",
                    "http://127.0.0.2:43121", "http://[::1]:43121", "http://2130706433:43121",
                    "http://127.0.0.1:43121/", BRIDGE + "/v1", BRIDGE + "?a=1", BRIDGE + "#x",
                    "http://user@127.0.0.1:43121", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                    "http://127.0.0.1:043121", BRIDGE + "\n", None):
            with self.subTest(url=url), self.assertRaises(Fault):
                render_host_config(codex_snapshot(), url, CAPABILITY)
        for home in ("/root", "/home/xiaolvji/../root", "/home/xiaolvji/", None):
            with self.subTest(home=home), self.assertRaises(Fault):
                render_host_config(codex_snapshot(), BRIDGE, CAPABILITY, home=home)

    def test_capability_is_only_opaque_bounded_ascii_and_never_in_evidence(self):
        for capability in ("short", "x" * 257, "x" * 31 + "\n", "x" * 31 + " ",
                           "Bearer " + "x" * 40, "x" * 31 + "é", "x" * 31 + "\x00", None):
            with self.subTest(capability=capability), self.assertRaises(Fault) as caught:
                render_host_config(codex_snapshot(), BRIDGE, capability)
            self.assertNotIn(str(capability), caught.exception.message)
        for snapshot in (codex_snapshot(), zcode_snapshot()):
            rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
            self.assertNotIn(CAPABILITY, json.dumps(rendered["evidence"]))
            self.assertNotIn(CAPABILITY, json.dumps(rendered["request_constraints"]))
            self.assertNotIn("apiKey", json.dumps(normalize_host_snapshot(snapshot)))

    def test_digests_bind_snapshot_and_exact_generated_data_per_attempt(self):
        for snapshot in (codex_snapshot(), zcode_snapshot()):
            first = render_host_config(snapshot, BRIDGE, CAPABILITY)
            same = render_host_config(copy.deepcopy(snapshot), BRIDGE, CAPABILITY)
            other_cap = render_host_config(snapshot, BRIDGE, "B" * 43)
            self.assertEqual(first, same)
            self.assertEqual(digest(snapshot), first["snapshot_digest"])
            self.assertEqual(first["snapshot_digest"], other_cap["snapshot_digest"])
            self.assertNotEqual(first["config_digest"], other_cap["config_digest"])
            self.assertEqual(digest({name: first[name] for name in ("files", "env", "argv_extra")}), first["config_digest"])
            other_model = copy.deepcopy(snapshot)
            other_model["model"] += "-changed"
            self.assertNotEqual(first["snapshot_digest"], render_host_config(other_model, BRIDGE, CAPABILITY)["snapshot_digest"])


@unittest.skipUnless(os.environ.get("XIAOLVJI_CODEX_CLI_SMOKE"),
                     "Set XIAOLVJI_CODEX_CLI_SMOKE for isolated local configuration loading only.")
class InstalledCodexHostConfigTests(unittest.TestCase):
    def test_rendered_config_and_cli_pins_load_without_login_or_model_request(self):
        binary = Path(os.environ["XIAOLVJI_CODEX_CLI_SMOKE"])
        self.assertTrue(binary.is_absolute() and binary.is_file())
        snapshot = codex_snapshot()
        snapshot["quality"].update(model_reasoning_summary="detailed", model_verbosity="high",
                                   service_tier="fast", model_context_window=200_000,
                                   model_auto_compact_token_limit=150_000)
        rendered = render_host_config(snapshot, BRIDGE, CAPABILITY)
        with tempfile.TemporaryDirectory(prefix="xiaolvji-host-loading-") as temporary:
            root = Path(temporary)
            for name in ("home", "codex", "config", "cache", "data", "state"):
                (root / name).mkdir()
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                   "CODEX_HOME": str(root / "codex"), "XDG_CONFIG_HOME": str(root / "config"),
                   "XDG_CACHE_HOME": str(root / "cache"), "XDG_DATA_HOME": str(root / "data"),
                   "XDG_STATE_HOME": str(root / "state"), "LANG": "C.UTF-8", CAPABILITY_ENV: CAPABILITY}
            config = root / "codex" / "config.toml"
            content = rendered["files"][0]["content"]

            def load(text):
                config.write_text(text, encoding="utf-8")
                with tempfile.TemporaryFile() as output:
                    result = subprocess.run([str(binary), "app-server", "--strict-config", "--print-startup-config",
                                             *rendered["argv_extra"]], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=subprocess.STDOUT, timeout=10, check=False)
                    output.seek(0)
                    data = output.read(131_073)
                self.assertLessEqual(len(data), 131_072)
                return result.returncode, data.decode("utf-8", "replace")

            code, output = load(content)
            self.assertEqual(0, code, output)
            records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            self.assertTrue(any("appServerVersion" in item and "configRequirements" in item for item in records))
            # Unknown field control proves strict parsing, not request success.
            code, output = load("model_max_output_tokens = 32000\n" + content)
            self.assertNotEqual(0, code, output)
            self.assertIn("model_max_output_tokens", output)
            self.assertFalse((root / "codex" / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
