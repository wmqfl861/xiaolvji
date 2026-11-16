from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from xiaolvji.adapters import build_command, parse_event, ZCODE_DISALLOWED_TOOLS
from xiaolvji.contracts import Fault
from xiaolvji.doctor import doctor


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-adapter-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def test_codex_prompt_uses_stdin_without_shell_or_model_override(self):
        prompt = 'Handle `quoted`; $(not a command) and \n newlines.'
        command = build_command("codex", self.workspace, prompt, "/opt/tools/codex")
        self.assertEqual(command["stdin_text"], prompt)
        self.assertNotIn(prompt, command["argv"])
        self.assertEqual(command["argv"], ["/opt/tools/codex", "exec", "--json", "--sandbox", "workspace-write",
                                            "--cd", str(self.workspace), "-c", "agents.enabled=false", "-"])
        self.assertEqual(command["env"], {})
        for dangerous in ("--yolo", "--full-auto", "--model", "--dangerously-bypass-hook-trust"):
            self.assertNotIn(dangerous, command["argv"])

    def test_zcode_explicit_guarded_and_native_tools_disabled(self):
        prompt = 'Implement one file; "quotes" remain literal.'
        command = build_command("zcode", {"path": str(self.workspace)}, prompt)
        argv = command["argv"]
        self.assertIn("--prompt=" + prompt, argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "guarded")
        self.assertEqual(argv[argv.index("--workflow-mode") + 1], "disabled")
        self.assertEqual(argv[argv.index("--disallowed-tools") + 1], ",".join(ZCODE_DISALLOWED_TOOLS))
        self.assertIsNone(command["stdin_text"])

    def test_zcode_prompt_is_one_literal_argument_even_when_it_looks_like_flags(self):
        for prompt in ("--help", "--disallowed-tools=Read", "--mode=unrestricted",
                       '修复模块\nKeep "quotes", spaces and --flags literal.'):
            with self.subTest(prompt=prompt):
                command = build_command("zcode", self.workspace, prompt)
                argv = command["argv"]
                self.assertEqual(["--prompt=" + prompt],
                                 [arg for arg in argv if arg.startswith("--prompt=")])
                self.assertNotIn(prompt, argv)
                self.assertNotIn("--prompt", argv)
                self.assertEqual(argv[argv.index("--disallowed-tools") + 1],
                                 ",".join(ZCODE_DISALLOWED_TOOLS))
                self.assertIsNone(command["stdin_text"])

    def test_zcode_prompt_limit_counts_utf8_bytes_without_truncation(self):
        # Windows also has a lower command-line limit; the shared UTF-8 byte
        # limit must still reject one byte too many on every platform.
        if os.name != "nt":
            prompt = "绿" * 40_000
            command = build_command("zcode", self.workspace, prompt)
            self.assertIn("--prompt=" + prompt, command["argv"])
            self.assertEqual(120_000, len(prompt.encode("utf-8")))
        for prompt in ("x" * 120_001, "绿" * 40_000 + "x"):
            with self.subTest(bytes=len(prompt.encode("utf-8"))):
                with self.assertRaises(Fault) as raised:
                    build_command("zcode", self.workspace, prompt)
                self.assertEqual("CAPABILITY_UNSUPPORTED", raised.exception.code)

    def test_bad_command_inputs_refused(self):
        cases = [("unknown", self.workspace, "ok"), ("codex", ".", "ok"),
                 ("codex", self.workspace / "missing", "ok"), ("codex", self.workspace, ""),
                 ("codex", self.workspace, "x\x00y"), ("codex", self.workspace, "x" * 1_048_577),
                 ("zcode", self.workspace, "x" * 120_001)]
        for args in cases:
            with self.subTest(args=(args[0], str(args[1]), len(args[2]))), self.assertRaises(Fault):
                build_command(*args)

    def test_zcode_windows_command_length_is_checked(self):
        with mock.patch("xiaolvji.adapters.os.name", "nt"):
            # Construct Path before mock: pathlib uses os.name for constructors.
            with mock.patch("xiaolvji.adapters.Path", return_value=self.workspace):
                with self.assertRaises(Fault):
                    build_command("zcode", str(self.workspace), "x" * 29_000)

    def test_model_turn_end_has_no_process_exit_authority(self):
        for engine, line in (("codex", '{"type":"turn.completed"}'),
                             ("codex", '{"type":"turn.failed"}'),
                             ("zcode", '{"type":"result","sessionId":"s1","response":"done"}')):
            event = parse_event(engine, line)
            self.assertTrue(event["terminal_report"])
            self.assertFalse(event["process_tree_terminated"])
            self.assertEqual(event["kind"], "turn_reported")

    def test_nonterminal_identity_is_observed_not_authorized(self):
        event = parse_event("codex", b'{"type":"thread.started","thread_id":"t1"}')
        self.assertEqual(event["session_id"], "t1")
        self.assertFalse(event["terminal_report"])
        self.assertIsNone(parse_event("codex", " \n"))

    def test_documented_codex_jsonl_lifecycle_does_not_attest_exit(self):
        # Official non-interactive-mode event shapes; these are synthetic data,
        # not proof of a completed live Codex session.
        lines = [
            '{"type":"thread.started","thread_id":"synthetic-thread"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"done"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
        ]
        events = [parse_event("codex", line) for line in lines]
        self.assertEqual([False, False, False, True], [e["terminal_report"] for e in events])
        self.assertTrue(all(e["process_tree_terminated"] is False for e in events))
        self.assertEqual("synthetic-thread", events[0]["session_id"])
        self.assertIsNone(events[-1]["session_id"])
        self.assertEqual(json.loads(lines[-1]), events[-1]["payload"])

    def test_invalid_ndjson_refused(self):
        lines = ['{"type":"x","type":"y"}', '{"type":"x","value":NaN}',
                 '[]', '{}', '{"type":1}', '{"type":"x","thread_id":4}',
                 '{"type":"x"}\n{"type":"y"}', b'\xff', 'x' * 1_048_577]
        for line in lines:
            with self.subTest(line=str(line)[:80]), self.assertRaises(Fault):
                parse_event("codex", line)

    def test_event_json_rejects_recursive_invalid_values_with_sanitized_faults(self):
        lines = [
            '{"type":"x","nested":{"metric":1e400}}',
            '{"type":"x","nested":{"metrics":[-1e400]}}',
            '{"type":"x","nested":{"text":"\\ud800"}}',
            '{"type":"x","nested":{"\\ud800":true}}',
            '{"type":"x","nested":{"private diagnostic":1,"private diagnostic":2}}',
            '{"type":"x","nested":' + "[" * 50 + "0" + "]" * 50 + "}",
        ]
        for engine in ("codex", "zcode"):
            for line in lines:
                with self.subTest(engine=engine, line=line[:100]):
                    with self.assertRaises(Fault) as raised:
                        parse_event(engine, line)
                    self.assertEqual({"code": "INVALID_EVENT",
                                      "message": "Host output is not one unambiguous JSON object."},
                                     raised.exception.as_dict())
                    self.assertTrue(raised.exception.__suppress_context__)

    def test_event_json_preserves_valid_nested_values(self):
        payload = {"type": "future.progress", "nested": {
            "metrics": [1e308, -1e308, 0, True, None], "text": "绿鸡 🐔"}}
        for engine in ("codex", "zcode"):
            with self.subTest(engine=engine):
                event = parse_event(engine, json.dumps(payload).encode("utf-8"))
                self.assertEqual(payload, event["payload"])
                self.assertFalse(event["process_tree_terminated"])

    def test_doctor_reports_missing_hosts_and_no_auth(self):
        with mock.patch("xiaolvji.doctor.shutil.which", return_value=None):
            result = doctor(profile="controlled_executor")
        self.assertEqual(result["effective_mode"], "blocked")
        self.assertFalse(result["permissions_changed"])
        self.assertFalse(result["authentication_attempted"])
        self.assertTrue(all(not host["integration_tested"] for host in result["host_capabilities"]))
        for engine in ("codex", "zcode"):
            self.assertIn(engine + ": executable unavailable", result["blocking_reasons"])


@unittest.skipUnless(os.environ.get("XIAOLVJI_CODEX_CLI_SMOKE"),
                     "Set XIAOLVJI_CODEX_CLI_SMOKE to an installed Codex binary for local config-loading checks.")
class InstalledCodexLoadingTests(unittest.TestCase):
    """Opt-in local loading only: no login, inference, hooks, or auth-file access."""

    def test_installed_cli_accepts_adapter_flags_and_boolean_agents_setting(self):
        executable = Path(os.environ["XIAOLVJI_CODEX_CLI_SMOKE"])
        self.assertTrue(executable.is_absolute() and executable.is_file())
        with tempfile.TemporaryDirectory(prefix="xiaolvji-codex-loading-") as temporary:
            root = Path(temporary)
            for name in ("home", "codex", "config", "cache", "data", "state"):
                (root / name).mkdir()
            # Construct a fresh environment. Never inherit credentials, proxy
            # tokens, existing CODEX_HOME, or the user's real home/config.
            env = {
                "PATH": str(executable.parent) + os.pathsep + "/usr/bin:/bin",
                "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
                "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
                "LANG": "C.UTF-8",
            }

            def run(*args):
                # Bound inspected output and runtime; even --help is external code.
                with tempfile.TemporaryFile() as output:
                    result = subprocess.run([str(executable), *args], cwd=root, env=env,
                                            stdin=subprocess.DEVNULL, stdout=output,
                                            stderr=subprocess.STDOUT, timeout=10, check=False)
                    output.seek(0)
                    data = output.read(131_073)
                self.assertLessEqual(len(data), 131_072)
                return result.returncode, data.decode("utf-8", "replace")

            code, version = run("--version")
            self.assertEqual(0, code, version)
            self.assertIn("codex-cli ", version)
            code, help_text = run("exec", "--help")
            self.assertEqual(0, code, help_text)
            for flag in ("--json", "--sandbox", "--cd", "--config"):
                self.assertIn(flag, help_text)
            self.assertIn("stdin", help_text)

            config = root / "codex" / "config.toml"
            config.write_text("[agents]\nenabled = false\n", encoding="utf-8")
            code, valid = run("app-server", "--strict-config", "--print-startup-config")
            self.assertEqual(0, code, valid)
            records = [json.loads(line) for line in valid.splitlines() if line.startswith("{")]
            self.assertTrue(any("appServerVersion" in record and "configRequirements" in record
                                for record in records))
            # A negative type control distinguishes schema loading from merely
            # accepting an arbitrary CLI key. It still does not inspect tools.
            config.write_text('[agents]\nenabled = "false"\n', encoding="utf-8")
            code, invalid = run("app-server", "--strict-config", "--print-startup-config")
            self.assertNotEqual(0, code, invalid)
            self.assertFalse((root / "codex" / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
