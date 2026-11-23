"""Inspect one installed local Codex coordinator using its App Server. No model turn."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
from pathlib import Path

from . import __version__
from .contracts import Fault, load_json
from .hooks import CODEX_EVENTS

PLUGIN = 'parallel-development'
READER_TOOLS = {'parallel_status', 'parallel_events', 'parallel_queue', 'parallel_metrics',
                'parallel_project_tree', 'parallel_read_file'}
FRAMESIZE = 4 * 1024 * 1024


class ProbeError(Exception):
    pass


@asynccontextmanager
async def session(executable, cwd):
    process = await asyncio.create_subprocess_exec(
        executable, 'app-server', '--stdio', cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=FRAMESIZE)
    request_id = 0

    async def send(value):
        process.stdin.write(json.dumps(value, allow_nan=False).encode() + b'\n')
        await process.stdin.drain()

    async def request(method, params):
        nonlocal request_id
        request_id += 1
        async with asyncio.timeout(25):
            await send({'id': request_id, 'method': method, 'params': params})
            for _ in range(512):
                line = await process.stdout.readline()
                if not line:
                    raise ProbeError('App Server closed its response stream.')
                msg = load_json(line, limit=FRAMESIZE)
                if not isinstance(msg, dict):
                    raise ProbeError('Invalid App Server message.')
                if 'method' in msg and 'id' not in msg:
                    continue  # asynchronous notification
                if 'method' in msg or type(msg.get('id')) is not int or msg['id'] != request_id:
                    raise ProbeError('Unexpected App Server request or response identifier.')
                if 'error' in msg:
                    reason = msg['error'].get('message', '') if isinstance(msg['error'], dict) else ''
                    if isinstance(reason, str) and 'NETLINK_ROUTE' in reason:
                        raise ProbeError(f'{method}: native filesystem sandbox could not initialize its network namespace.')
                    raise ProbeError(f'App Server rejected {method}.')
                if not isinstance(msg.get('result'), dict):
                    raise ProbeError(f'Invalid {method} response.')
                return msg['result']
            raise ProbeError('App Server notification budget exceeded.')

    try:
        await request('initialize', {'clientInfo': {'name': 'xiaolvji-install-check', 'version': __version__},
                                     'capabilities': {'experimentalApi': True}})
        await send({'method': 'initialized'})
        yield request
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def require(condition, message):
    if not condition:
        raise ProbeError(message)


async def reader_inventory(request, plugin_id, thread_id=None):
    params = {'serverName': PLUGIN, 'detail': 'toolsAndAuthOnly'}
    if thread_id is not None:
        params['threadId'] = thread_id
    servers = []
    seen_cursors = set()
    for _ in range(8):
        listing = await request('mcpServerStatus/list', params)
        servers.extend(item for item in listing['data'] if item['name'] == PLUGIN)
        cursor = listing.get('nextCursor')
        if cursor is None:
            break
        require(isinstance(cursor, str) and 0 < len(cursor) <= 4096 and cursor not in seen_cursors,
                'Invalid or repeated MCP inventory cursor.')
        seen_cursors.add(cursor)
        params['cursor'] = cursor
    else:
        raise ProbeError('MCP inventory page budget exceeded.')
    require(len(servers) == 1 and servers[0].get('pluginId') == plugin_id,
            'Could not uniquely identify the installed plugin MCP server.')
    server = servers[0]
    require(not server.get('toolsError') and set(server['tools']) == READER_TOOLS,
            'The installed MCP server must initialize against the controller using the reader identity.')
    return sorted(server['tools'])


async def inspect_install(executable, marketplace, cwd, *, thread_call=False):
    report = {'schema_version': 1, 'status': 'blocked', 'runtime_version': __version__,
              'stage': 'initialize', 'thread_call_requested': thread_call, 'thread_status_call_verified': False,
              'model_turn_started': False, 'hook_execution_verified': False}
    try:
        cwd = Path(cwd).resolve()
        marketplace = Path(marketplace).resolve()
        if marketplace.is_dir():
            marketplace = marketplace / '.agents/plugins/marketplace.json'
        async with session(executable, cwd) as request:
            report['stage'] = 'installed_plugin'
            detail = (await request('plugin/read', {'marketplacePath': str(marketplace), 'pluginName': PLUGIN}))['plugin']
            summary = detail['summary']
            plugin_id = f"{PLUGIN}@{detail['marketplaceName']}"
            require(summary['id'] == plugin_id and summary['installed'] is True and summary['enabled'] is True,
                    'The selected marketplace plugin must be installed and enabled.')
            require(summary['localVersion'] == __version__, 'The installed version differs from this verifier.')
            require(PLUGIN in detail['mcpServers'] and any(skill['name'] == f'{PLUGIN}:{PLUGIN}' and skill['enabled']
                        for skill in detail['skills']), 'The plugin must declare its MCP server and enabled skill.')
            report.update(plugin_id=plugin_id, installed_version=summary['localVersion'], skill_enabled=True)

            report['stage'] = 'reader_tools'
            report['reader_tools'] = await reader_inventory(request, plugin_id)

            report['stage'] = 'registered_hooks'
            hooks = (await request('hooks/list', {'cwds': [str(cwd)]}))['data']
            inspected = [hook for row in hooks if Path(row['cwd']) == cwd for hook in row['hooks']
                         if hook['pluginId'] == plugin_id]
            expected = {event[0].lower() + event[1:] for event in CODEX_EVENTS}
            report['hooks'] = [{'event': hook['eventName'], 'trust': hook['trustStatus'], 'enabled': hook['enabled']}
                               for hook in sorted(inspected, key=lambda hook: hook['eventName'])]
            require(Counter(hook['event'] for hook in report['hooks']) == Counter(expected),
                    'Expected each of the seven plugin hooks exactly once. Use the local marketplace distribution.')
            require(all(hook['enabled'] is True for hook in inspected), 'A declared plugin hook is disabled.')
            report['all_hooks_trusted'] = all(hook['trustStatus'] in ('trusted', 'managed') for hook in inspected)

            if thread_call:
                report['stage'] = 'thread_start'
                thread = (await request('thread/start', {'cwd': str(cwd), 'ephemeral': True}))['thread']['id']
                report['stage'] = 'thread_reader_tools'
                await reader_inventory(request, plugin_id, thread)
                report['stage'] = 'thread_status_call'
                result = await request('mcpServer/tool/call', {'server': PLUGIN, 'threadId': thread,
                                                              'tool': 'parallel_status', 'arguments': {}})
                require(not result.get('isError') and len(result['content']) == 1 and result['content'][0]['type'] == 'text',
                        'The status tool returned an error or an unexpected result.')
                capacity = load_json(result['content'][0]['text'])['capacity']
                require(type(capacity['limit']) is int and capacity['limit'] > 0, 'The controller returned invalid capacity.')
                report.update(thread_status_call_verified=True, controller_limit=capacity['limit'])
        report.update(status='passed', stage='complete')
    except (ProbeError, OSError, TimeoutError, Fault, AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        report['reason'] = str(exc) if isinstance(exc, ProbeError) else f'Local host check failed ({type(exc).__name__}).'
    return report
