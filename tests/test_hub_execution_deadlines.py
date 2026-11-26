"""Real SDK deadlines start after Hub admission; hanging tools time out without replay."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from agents.mcp import MCPServerStdio
from mcp.shared.exceptions import McpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_transport import HubSettings, HubTaskServer, TaskBridge


@pytest.mark.asyncio
async def test_real_stdio_execution_deadline_excludes_queue_and_returns_hanging_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv('MCPHUB_CALLBACK_HOST', '127.0.0.1')
    marker = tmp_path / 'events.jsonl'
    fixture = tmp_path / 'mcp_fixture.py'
    fixture.write_text('''import asyncio,json,sys,os
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool,TextContent
server=Server('deadline-fixture')
marker=Path(sys.argv[1])
marker.with_suffix('.pid').write_text(str(os.getpid()))
def log(value):
 with marker.open('a') as f:f.write(json.dumps(value)+'\\n')
@server.list_tools()
async def tools():
 return [Tool(name='execute',inputSchema={'type':'object','properties':{'hang':{'type':'boolean'}}})]
@server.call_tool()
async def call(name,args):
 log({'event':'started','hang':args.get('hang',False)})
 if args.get('hang'):
  try:await asyncio.Event().wait()
  finally:log({'event':'cancelled'})
 await asyncio.sleep(.02)
 return [TextContent(type='text',text='original-result')]
async def main():
 async with stdio_server() as streams:
  await server.run(*streams,server.create_initialization_options())
asyncio.run(main())
''')
    leases = {}
    dispatches = []
    async def register(request):
        leases['one'] = await request.json()
        return web.json_response({'success': True, 'data': {
            'id': 'one', 'token': 'lease-key', 'path': '/gateway/v1/tasks/one'}})
    async def dispatch(request):
        dispatches.append(request.match_info['operation'])
        body = await request.json()
        # Admission exceeds the execution budget, but no local MCP request
        # has been made yet. Delayed admission alone must not expire it.
        await asyncio.sleep(1.2)
        lease = leases['one']
        async with httpx.AsyncClient() as client:
            result = await client.post(lease['url'] + '/' + request.match_info['operation'],
                headers={'Authorization': 'Bearer ' + lease['token']}, json=body)
        return web.json_response(result.json(), status=result.status_code)
    async def remove(request):
        leases.clear()
        return web.json_response({'success': True})
    app = web.Application()
    app.router.add_post('/api/task-connections', register)
    app.router.add_delete('/api/task-connections/one', remove)
    app.router.add_post('/gateway/v1/tasks/one/{operation}', dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    settings = HubSettings(f'http://127.0.0.1:{runner.addresses[0][1]}', 'key',
                           'toolathlon', frozenset(), '', '-')
    bridge = TaskBridge(settings)
    backend = MCPServerStdio(params={'command': sys.executable, 'args': ['-B', str(fixture), str(marker)]},
                             client_session_timeout_seconds=1)
    try:
        async with HubTaskServer('k8s', backend, bridge, {'client_session_timeout_seconds': 1}) as server:
            before = time.monotonic()
            assert (await server.call_tool('execute', {})).content[0].text == 'original-result'
            assert time.monotonic() - before >= 1.2
            before = time.monotonic()
            with pytest.raises(McpError, match='(?i)timed out'):
                await asyncio.wait_for(server.call_tool('execute', {'hang': True}), 6)
            assert 2 <= time.monotonic() - before < 5
            # The pinned SDK returns its native timeout error. It does not
            # guarantee remote process cancellation; do not invent that contract.
            assert (await server.call_tool('execute', {})).content[0].text == 'original-result'
            records = [json.loads(line) for line in marker.read_text().splitlines()]
            assert sum(r.get('hang') is True for r in records) == 1, 'hanging tool must never replay'
            assert dispatches.count('call-tool') == 3
            assert not any(bridge.active.values())
        assert not leases and not bridge.bindings and bridge.runner is None
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.with_suffix('.pid').read_text()), 0)
    finally:
        await runner.cleanup()
