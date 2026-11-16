"""Subprocess smoke tests against the real source CLI and generated packages.

Windows packages are inspected and their Python program is run on Linux here;
these tests do not represent a Windows host or native Codex/ZCode integration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from xiaolvji.installer import build_plugin
from xiaolvji import __version__

REPO = Path(__file__).resolve().parents[1]


class RealCliPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="xiaolvji-real-cli-")
        cls.root = Path(cls.temporary.name)
        cls.launcher = REPO / "bin" / "parallel-dev"
        cls.packages = {}
        for adapter in ("codex", "zcode"):
            for target in ("posix", "windows"):
                path = cls.root / f"{adapter} {target} package"
                build_plugin(adapter, path, REPO, target_platform=target)
                cls.packages[adapter, target] = path

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))
        self.state = self.test_root / "state"
        self.project = self.test_root / "project"
        self.project.mkdir()
        self.env = {**os.environ, "PARALLEL_DEV_STATE_DIR": str(self.state)}

    def run_cli(self, *args, package=None, input=None):
        launcher = (package / "bin" / "parallel-dev") if package else self.launcher
        return subprocess.run([sys.executable, "-I", str(launcher), *args], cwd=self.test_root,
                              input=input, text=True, capture_output=True, env=self.env, timeout=30)

    def initialize_project(self):
        result = self.run_cli("init", "--global-limit", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("project", "enable", "project-1", str(self.project), "--limit", "2",
                              "--profile", "cooperative_native", "--acknowledge-cooperative")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_and_each_generated_package_help_and_version(self):
        for package in (None, *self.packages.values()):
            with self.subTest(package=package):
                help_result = self.run_cli("--help", package=package)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("check-runner", help_result.stdout)
                self.assertIn("queue", help_result.stdout)
                self.assertIn("metrics", help_result.stdout)
                version = self.run_cli("--version", package=package)
                self.assertEqual(version.returncode, 0, version.stderr)
                self.assertEqual(version.stdout.strip(), __version__)
                if package:
                    self.assertEqual(__version__, json.loads((package / "PACKAGE.json").read_text())["version"])
                    for manifest in package.rglob("plugin.json"):
                        self.assertEqual(__version__, json.loads(manifest.read_text())["version"], manifest)

    def test_real_packaged_doctor_strict_mode_is_blocked(self):
        for (adapter, target), package in self.packages.items():
            with self.subTest(adapter=adapter, target=target):
                result = self.run_cli("doctor", "--engine", adapter, package=package)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["effective_mode"], "blocked")
                self.assertEqual(report["offline_executor_supported"], report["executor"]["strict_supported"])
                self.assertFalse(report["model_transport"]["strict_supported"])
                self.assertEqual("none", report["model_transport"]["network_policy"])
                self.assertFalse(report["authentication_attempted"])
                self.assertFalse(report["permissions_changed"])

    def test_run_watch_waits_for_work_until_explicit_timeout(self):
        self.initialize_project()
        result = self.run_cli("run", "project-1", "--watch", "--timeout", "0.05")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["watch"])
        self.assertEqual(report["stop_reason"], "timeout_or_cancel")
        self.assertEqual(report["status"]["capacity"]["used"], 0)
        self.assertIn('"type":"idle_waiting"', result.stderr)

    def test_source_and_generated_packages_queue_and_metrics_are_read_only(self):
        self.initialize_project()
        before = json.loads(self.run_cli("status").stdout)
        events_before = json.loads(self.run_cli("events").stdout)
        identity = self.state / "identities" / "reader.key"
        for package in (None, *self.packages.values()):
            with self.subTest(package=package):
                queue = self.run_cli("--identity-file", str(identity), "queue", "--project", "project-1",
                                     "--limit", "1", package=package)
                self.assertEqual(0, queue.returncode, queue.stderr + queue.stdout)
                queue_data = json.loads(queue.stdout)
                self.assertTrue(queue_data["advisory"])
                self.assertEqual([], queue_data["entries"])
                self.assertEqual(before["capacity"], queue_data["capacity"])
                metrics = self.run_cli("--identity-file", str(identity), "metrics", "--project", "project-1",
                                       package=package)
                self.assertEqual(0, metrics.returncode, metrics.stderr + metrics.stdout)
                metrics_data = json.loads(metrics.stdout)
                self.assertTrue(metrics_data["diagnostic_only"])
                self.assertEqual("project-1", metrics_data["project_id"])
                self.assertEqual(0, metrics_data["population"]["tasks"])
                self.assertEqual(before["capacity"], metrics_data["capacity"]["global"])
        self.assertEqual(before, json.loads(self.run_cli("status").stdout))
        self.assertEqual(events_before, json.loads(self.run_cli("events").stdout))

    def test_queue_and_metrics_reject_invalid_bounds_and_mutation_options(self):
        self.initialize_project()
        before = json.loads(self.run_cli("status").stdout)
        events_before = json.loads(self.run_cli("events").stdout)
        commands = [
            ("queue", "--limit", "0"), ("queue", "--limit", "201"),
            ("queue", "--limit", "true"), ("queue", "--limit", "null"),
            ("queue", "--limit", "1.5"), ("queue", "--project", "../invalid"),
            ("queue", "--reserve"), ("queue", "--start"), ("queue", "--lim", "1"),
            ("metrics", "--limit", "1"), ("metrics", "--decision", "approve"),
            ("metrics", "--project", "../invalid"), ("metrics", "--proj", "project-1"),
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.run_cli(*command)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(before, json.loads(self.run_cli("status").stdout))
        self.assertEqual(events_before, json.loads(self.run_cli("events").stdout))

    def test_unenrolled_valid_hook_is_json_noop_for_each_package(self):
        for (adapter, target), package in self.packages.items():
            result = self.run_cli("hook", "--adapter", adapter, package=package,
                                  input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(self.project)}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
            self.assertFalse(self.state.exists())

    def test_enrolled_session_injects_and_pretool_spawn_denies(self):
        self.initialize_project()
        for adapter in ("codex", "zcode"):
            package = self.packages[adapter, "posix"]
            start = self.run_cli("hook", "--adapter", adapter, package=package,
                                 input=json.dumps({"hookEventName": "SessionStart", "cwd": str(self.project)}))
            self.assertEqual(start.returncode, 0, start.stderr)
            context = json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("project-1", context)
            self.assertIn("authoritative controller ledger", context)
            denied = self.run_cli("hook", "--adapter", adapter, package=package,
                                  input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(self.project),
                                                    "session_id": "a-parent-session", "tool_name": "Agent", "tool_input": {}}))
            self.assertEqual(denied.returncode, 0, denied.stderr)
            self.assertEqual(json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            stop = self.run_cli("hook", "--adapter", adapter, package=package,
                                input=json.dumps({"hook_event_name": "Stop", "cwd": str(self.project)}))
            self.assertEqual(json.loads(stop.stdout), {})

    def test_malformed_hook_is_nonzero_and_structured_denial(self):
        for adapter in ("codex", "zcode"):
            package = self.packages[adapter, "posix"]
            for payload in ('not-json', '{"hook_event_name":"PreToolUse","tool_name":"Agent","tool_name":"Bash"}',
                            '{"hook_event_name":"PreToolUse","hookEventName":"Stop","tool_name":"Agent"}'):
                result = self.run_cli("hook", "--adapter", adapter, package=package, input=payload)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertNotIn("Traceback", result.stderr)

    def test_packaged_policy_roundtrip_uses_embedded_resources(self):
        original = b"# Existing project rule\r\nRun the agreed tests."
        target = self.project / "AGENTS.md"
        target.write_bytes(original)
        package = self.packages["codex", "posix"]
        for action in ("inspect", "apply", "remove"):
            result = self.run_cli("policy", action, "--project", str(self.project), package=package)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertEqual(target.read_bytes(), original)

    def test_actual_packaged_mcp_defaults_to_read_only(self):
        self.initialize_project()
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "parallel_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "parallel_register_task", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "parallel_queue", "arguments": {"project_id": "project-1", "limit": 1}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "parallel_metrics", "arguments": {"project_id": "project-1"}}},
        ]
        wire = "\n".join(json.dumps(request) for request in requests) + "\n"
        before = json.loads(self.run_cli("status").stdout)
        events_before = json.loads(self.run_cli("events").stdout)
        for package in self.packages.values():
            result = self.run_cli("mcp", package=package, input=wire)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = {message["id"]: message for message in map(json.loads, result.stdout.splitlines())}
            self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
            names = {tool["name"] for tool in replies[2]["result"]["tools"]}
            self.assertEqual(names, {"parallel_status", "parallel_queue", "parallel_metrics", "parallel_events",
                                     "parallel_project_tree", "parallel_read_file"})
            self.assertFalse(replies[3]["result"]["isError"])
            self.assertTrue(replies[4]["result"]["isError"])
            self.assertFalse(replies[5]["result"]["isError"])
            self.assertFalse(replies[6]["result"]["isError"])
            self.assertTrue(json.loads(replies[5]["result"]["content"][0]["text"])["advisory"])
            self.assertTrue(json.loads(replies[6]["result"]["content"][0]["text"])["diagnostic_only"])
        self.assertEqual(before, json.loads(self.run_cli("status").stdout))
        self.assertEqual(events_before, json.loads(self.run_cli("events").stdout))

    def test_mcp_layout_and_no_credentials_in_each_distribution(self):
        for (adapter, target), package in self.packages.items():
            mcp_name = "mcp.json" if adapter == "codex" else ".mcp.json"
            configuration = json.loads((package / mcp_name).read_text())
            server = configuration["mcpServers"]["parallel-development"]
            self.assertEqual(server["type"], "stdio")
            self.assertEqual(server["command"], "py" if target == "windows" else "python3")
            self.assertEqual(server["args"][-1], "mcp")
            self.assertNotIn("--identity-file", server["args"])
            self.assertNotIn("env", server)
            if target == "windows":
                self.assertEqual(server["args"][0], "-3")
            self.assertFalse(list(package.rglob("*.key")))
            self.assertFalse(list(package.rglob("authority.json")))
            self.assertFalse(list(package.rglob("ledger.sqlite3")))
            self.assertFalse(list(package.rglob("*.exe")))


if __name__ == "__main__":
    unittest.main()
