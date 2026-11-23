"""Native package installation with the real system policy and a disposable account view."""
from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from xiaolvji import codex_plugin_probe as probe
from xiaolvji import __version__
from xiaolvji.contracts import Fault
from xiaolvji.installer import build_plugin

ROOT = Path(__file__).resolve().parents[1]
CLI = os.environ.get('XIAOLVJI_CODEX_INSTALL_SMOKE')


class MarketplaceLayoutTests(unittest.TestCase):
    def test_native_and_portable_layouts_have_separate_loader_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            for target in ('windows', 'posix'):
                for native in (False, True):
                    with self.subTest(target=target, native=native):
                        pkg = Path(directory) / f'{target}-{native}'
                        build_plugin('codex', pkg, ROOT, target, codex_marketplace=native)
                        manifest = json.loads((pkg / '.codex-plugin/plugin.json').read_text())
                        server = json.loads((pkg / manifest['mcpServers']).read_text())['mcpServers'][probe.PLUGIN]
                        self.assertEqual(['PARALLEL_DEV_STATE_DIR'], server['env_vars'])
                        self.assertEqual('.', server['cwd'])
                        expected = ['bin/parallel-dev', 'mcp']
                        self.assertEqual((['-3'] if target == 'windows' else []) + expected, server['args'])
                        self.assertEqual('py' if target == 'windows' else 'python3', server['command'])
                        self.assertEqual(not native, (pkg / 'plugin.json').exists())
                        self.assertEqual(not native, (pkg / 'mcp.json').exists())
                        index = pkg / '.agents/plugins/marketplace.json'
                        self.assertEqual(native, index.exists())
                        if native:
                            entry, = json.loads(index.read_text())['plugins']
                            self.assertEqual({'source': 'local', 'path': './'}, entry['source'])
                            self.assertEqual(probe.PLUGIN, entry['name'])

    def test_zcode_rejects_the_codex_marketplace_option_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with self.assertRaises(Fault):
                build_plugin('zcode', output, ROOT, codex_marketplace=True)
            self.assertFalse(output.exists())


@unittest.skipUnless(sys.platform == 'linux' and CLI,
                     'Set XIAOLVJI_CODEX_INSTALL_SMOKE to the local Linux Codex executable for native install acceptance')
class InstalledMarketplaceTests(unittest.TestCase):
    def setUp(self):
        import pwd
        temp = tempfile.TemporaryDirectory(prefix='xiaolvji-codex-install-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pkg = self.root / 'market'
        self.account = self.root / 'account'
        self.account.mkdir()
        self.work = self.root / 'work'
        self.work.mkdir()
        user_dir = pwd.getpwuid(os.getuid()).pw_dir
        self.codex = '/opt/codex-fixture'
        self.box = ['unshare', '--user', '--map-current-user', '--net', 'bwrap', '--die-with-parent', '--new-session',
                    '--unshare-user', '--unshare-ipc', '--unshare-pid', '--unshare-uts', '--clearenv',
                    '--ro-bind', '/usr', '/usr', '--ro-bind', '/etc', '/etc', '--ro-bind', str(Path(CLI).resolve()), self.codex]
        for name in ('bin', 'lib', 'lib64', 'sbin'):
            path = Path('/') / name
            if path.is_symlink():
                self.box.extend(['--symlink', os.readlink(path), str(path)])
            elif path.is_dir():
                self.box.extend(['--ro-bind', str(path), str(path)])
        self.box.extend(['--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp', '--dir', '/run',
                         '--bind', str(self.account), user_dir, '--bind', str(self.work), '/work',
                         '--ro-bind', str(self.pkg), '/local market', '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
                         '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'PARALLEL_DEV_STATE_DIR', '/work/controller with spaces',
                         '--chdir', '/work', '--'])

    def run_guest(self, *argv, input=None, expected=0):
        result = subprocess.run([*self.box, *argv], text=True, input=input, capture_output=True, timeout=50)
        self.assertEqual(expected, result.returncode, (result.stdout + result.stderr)[-1400:])
        return result.stdout

    def install(self):
        self.run_guest(self.codex, 'plugin', 'marketplace', 'add', '/local market', '--json')
        value = json.loads(self.run_guest(self.codex, 'plugin', 'add', 'parallel-development@xiaolvji-local', '--json'))
        self.assertEqual(__version__, value['version'])
        return value['installedPath']

    def test_installed_coordinator_inventory_and_reader_status(self):
        build_plugin('codex', self.pkg, ROOT, codex_marketplace=True)
        self.run_guest('python3', '/local market/bin/parallel-dev', 'init', '--global-limit', '7')
        installed = self.install()
        args = ['python3', installed + '/bin/parallel-dev', 'host', 'inspect-plugin', '--codex', self.codex, '--marketplace', '/local market', '--cwd', '/work']
        evidence = json.loads(self.run_guest(*args))
        self.assertEqual('passed', evidence['status'])
        self.assertEqual({'untrusted'}, {hook['trust'] for hook in evidence['hooks']})
        self.assertFalse(evidence['thread_status_call_verified'])
        self.assertEqual(probe.READER_TOOLS, set(evidence['reader_tools']))
        # Exercise the installed server directly as well. App Server inventory
        # and an optional thread-owned tool call have separate evidence levels.
        frames = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                   'capabilities': {}, 'clientInfo': {'name': 'xiaolvji-test', 'version': '1'}}},
                  {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                  {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'parallel_status', 'arguments': {}}}]
        out = self.run_guest('python3', installed + '/bin/parallel-dev', 'mcp', input=''.join(json.dumps(f) + '\n' for f in frames))
        result = [json.loads(line) for line in out.splitlines()][-1]['result']
        self.assertFalse(result['isError'])
        self.assertEqual(7, json.loads(result['content'][0]['text'])['capacity']['limit'])

    def test_portable_overlay_forwards_only_the_controller_directory(self):
        build_plugin('codex', self.pkg, ROOT)
        index = self.pkg / '.agents/plugins/marketplace.json'
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({'name': 'xiaolvji-local', 'plugins': [
            {'name': probe.PLUGIN, 'source': {'source': 'local', 'path': './'}}]}))
        self.run_guest('python3', '/local market/bin/parallel-dev', 'init', '--global-limit', '3')
        installed = self.install()
        args = ['python3', installed + '/bin/parallel-dev', 'host', 'inspect-plugin', '--codex', self.codex,
                '--marketplace', '/local market', '--cwd', '/work']
        evidence = json.loads(self.run_guest(*args, expected=2))
        self.assertEqual('registered_hooks', evidence['stage'])
        self.assertEqual([], evidence['hooks'])
        self.assertEqual(sorted(probe.READER_TOOLS), evidence['reader_tools'])


class ThreadProbeTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, *, thread_plugin='parallel-development@local', tool_error=False, duplicate_hook=False):
        calls = []
        cwd = Path('/work')
        plugin_id = 'parallel-development@local'
        hooks = [{'eventName': event[0].lower() + event[1:], 'enabled': True, 'trustStatus': 'untrusted',
                  'pluginId': plugin_id} for event in probe.CODEX_EVENTS]
        if duplicate_hook:
            hooks.append(hooks[0])

        async def request(method, params):
            calls.append((method, params))
            if method == 'plugin/read':
                return {'plugin': {'marketplaceName': 'local', 'summary': {'id': plugin_id, 'enabled': True,
                        'installed': True, 'localVersion': __version__}, 'mcpServers': [probe.PLUGIN], 'skills': [
                        {'name': 'parallel-development:parallel-development', 'enabled': True}]}}
            if method == 'hooks/list':
                return {'data': [{'cwd': str(cwd), 'hooks': hooks}]}
            if method == 'mcpServerStatus/list':
                return {'data': [{'name': probe.PLUGIN, 'pluginId': thread_plugin if 'threadId' in params else plugin_id,
                                  'tools': dict.fromkeys(probe.READER_TOOLS)}]}
            if method == 'thread/start':
                return {'thread': {'id': 'fixture-thread'}}
            self.assertEqual('mcpServer/tool/call', method)
            self.assertEqual({'server': probe.PLUGIN, 'threadId': 'fixture-thread', 'tool': 'parallel_status', 'arguments': {}}, params)
            return {'isError': tool_error, 'content': [{'type': 'text', 'text': '{"capacity":{"limit":7}}'}]}

        @asynccontextmanager
        async def protocol_fixture(*args):
            yield request

        with mock.patch.object(probe, 'session', protocol_fixture):
            report = await probe.inspect_install('codex', Path('/market'), cwd, thread_call=True)
        return report, [name for name, _ in calls]

    async def test_exact_thread_and_server_ownership_required_before_call(self):
        report, calls = await self.inspect(thread_plugin='other@local')
        self.assertEqual('thread_reader_tools', report['stage'])
        self.assertFalse(report['thread_status_call_verified'])
        self.assertNotIn('mcpServer/tool/call', calls)

    async def test_only_a_successful_status_payload_verifies_the_thread(self):
        report, _ = await self.inspect(tool_error=True)
        self.assertEqual('blocked', report['status'])
        self.assertFalse(report['thread_status_call_verified'])
        report, calls = await self.inspect()
        self.assertTrue(report['thread_status_call_verified'])
        self.assertEqual(7, report['controller_limit'])
        self.assertNotIn('turn/start', calls)
        self.assertFalse(report['all_hooks_trusted'])

    async def test_same_named_server_on_a_later_page_makes_the_target_ambiguous(self):
        server = {'name': probe.PLUGIN, 'pluginId': 'parallel-development@local', 'tools': dict.fromkeys(probe.READER_TOOLS)}
        pages = mock.AsyncMock(side_effect=[{'data': [server], 'nextCursor': 'more'},
                                           {'data': [{**server, 'pluginId': None}]}])
        with self.assertRaisesRegex(probe.ProbeError, 'uniquely'):
            await probe.reader_inventory(pages, server['pluginId'], 'fixture-thread')
        self.assertEqual(2, pages.await_count)

    async def test_duplicate_hooks_stop_before_starting_a_thread(self):
        report, calls = await self.inspect(duplicate_hook=True)
        self.assertEqual('registered_hooks', report['stage'])
        self.assertEqual('blocked', report['status'])
        self.assertNotIn('thread/start', calls)
