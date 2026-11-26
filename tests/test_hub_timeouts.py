"""Hub queues must not consume former direct-mode read timers."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
import requests
from aiohttp import web
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_timeouts import hub_http_timeout, hub_requests_timeout
from utils.mcp.hub_github import GithubAPIProxy, HOST
from utils.mcp.hub_sdk_http import _send_adapter
from utils.mcp.hub_notion import NotionTransport


def test_hub_transport_waits_keep_finite_connection_limits():
    timeout = hub_http_timeout()
    assert timeout.read == timeout.write == 915
    assert timeout.connect == timeout.pool == 15
    assert hub_requests_timeout() == (15, 915)


@pytest.mark.asyncio
async def test_github_proxy_waits_for_account_admission_beyond_old_deadline(monkeypatch):
    monkeypatch.setenv('MCPHUB_RUN_ID', 'run-a')
    monkeypatch.setenv('MCPHUB_RUN_TOKEN', 'run-secret')
    calls = []
    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0.06)
        return web.json_response({'ok': True})
    app = web.Application()
    app.router.add_route('*', '/api/workspace-runs/run-a/http/github/api/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    settings = SimpleNamespace(root=f'http://127.0.0.1:{runner.addresses[0][1]}', headers={})
    proxy = await GithubAPIProxy(settings, 0.01).start()
    try:
        async with httpx.AsyncClient(proxy=proxy.proxy_url, trust_env=False, timeout=2) as client:
            response = await client.get('http://' + HOST + '/api/v3/user',
                                        headers={'Authorization': 'Bearer ' + proxy.token})
        assert response.json() == {'ok': True}
        assert len(calls) == 1
    finally:
        await proxy.close()
        await runner.cleanup()


def test_mapped_sdk_http_waits_change_only_the_hub_route(monkeypatch):
    import utils.mcp.hub_sdk_http as sdk
    calls = []
    monkeypatch.setattr(sdk, 'hub_enabled', lambda: True)
    def send(self, request, **kwargs):
        calls.append((request.url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        response.headers = {}
        return response
    wrapped = _send_adapter(send)
    monkeypatch.setattr(sdk, 'mapping', lambda *args: None)
    session = requests.Session()
    req = requests.Request('GET', 'https://direct.test/').prepare()
    wrapped(session, req, timeout=10, allow_redirects=False)
    assert calls[-1][1]['timeout'] == 10
    monkeypatch.setattr(sdk, 'mapping', lambda *args: ('http://hub.test/route', {}))
    wrapped(session, req, timeout=10, allow_redirects=False)
    assert calls[-1][0] == 'http://hub.test/route'
    assert calls[-1][1]['timeout'] == (15, 625)
    assert calls[-1][0] == 'http://hub.test/route'
    assert calls[-1][1]['allow_redirects'] is False
    assert len(calls) == 2


def test_notion_hub_transport_does_not_keep_sdk_direct_timeout(monkeypatch):
    import utils.mcp.hub_notion as notion
    monkeypatch.setattr(notion, 'notion_endpoint', lambda *args: (
        SimpleNamespace(key='hub-key', token='run-token'), 'http://hub.test/route'))
    seen = []
    def send(request):
        seen.append(request)
        return httpx.Response(200, json={}, request=request)
    transport = NotionTransport(httpx.MockTransport(send))
    original = httpx.Request('POST', 'https://api.notion.com/v1/query', content=b'{}',
                             extensions={'timeout': {'connect': 5, 'read': 10, 'write': 10, 'pool': 5}})
    try:
        assert transport.handle_request(original).status_code == 200
        assert seen[0].extensions['timeout']['read'] == 625
        assert seen[0].headers['X-MCPHub-Upstream-Timeout-Ms'] == '10000'
        assert seen[0].extensions['timeout']['connect'] == 15
        assert original.extensions['timeout']['read'] == 10
        assert len(seen) == 1
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_disconnect_cancels_waiting_task_backend_without_replaying(monkeypatch):
    monkeypatch.setenv("MCPHUB_CALLBACK_HOST", "127.0.0.1")
    from utils.mcp.hub_transport import HubSettings, TaskBridge
    from agents.mcp import MCPServer
    from mcp.types import Tool
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = []
    class Backend(MCPServer):
        @property
        def name(self): return 'github'
        async def connect(self): pass
        async def cleanup(self): pass
        async def list_tools(self): return [Tool(name='write_file', inputSchema={'type':'object'})]
        async def call_tool(self, name, arguments):
            calls.append(name)
            started.set()
            try: await asyncio.Event().wait()
            finally: stopped.set()
    settings = HubSettings('http://hub.test','group-key','toolathlon',frozenset(),'','-')
    bridge = TaskBridge(settings)
    backend = Backend()
    binding, token, url = await bridge.bind('github', backend, await backend.list_tools())
    try:
        async with httpx.AsyncClient(timeout=hub_http_timeout()) as client:
            pending = asyncio.create_task(client.post(url + '/call-tool',
                headers={'Authorization': 'Bearer ' + token},
                json={'tool_name':'github_write_file','tool_args':{}}))
            await asyncio.wait_for(started.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError): await pending
            await asyncio.wait_for(stopped.wait(), 2)
        assert calls == ['write_file']
    finally:
        await bridge.unbind(binding)


@pytest.mark.asyncio
async def test_local_browser_stdio_deadline_returns_through_hub_callback(monkeypatch):
    """Exercise SDK timeout -> authenticated callback error -> caller, no live Hub."""
    from agents.mcp import MCPServerStdio
    from utils.mcp.hub_transport import HubSettings, TaskBridge
    from test_local_browser_timeout import initialize_local_server
    kwargs = initialize_local_server(True, 'playwright_with_chunk', timeout=.15)
    code = '''import json,sys,time
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 if r['method']=='initialize':
  result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif r['method']=='tools/list':
  result={'tools':[{'name':'browser_evaluate','inputSchema':{'type':'object'}}]}
 else:
  time.sleep(.5)
  result={'content':[{'type':'text','text':'late'}],'isError':False}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''
    kwargs['params'] = {'command':sys.executable, 'args':['-B','-c',code]}
    monkeypatch.setenv('MCPHUB_CALLBACK_HOST','127.0.0.1')
    bridge = TaskBridge(HubSettings('http://hub.test','key','toolathlon',frozenset(),'','_'))
    async with MCPServerStdio(**kwargs) as backend:
        binding, token, url = await bridge.bind('playwright_with_chunk',backend,await backend.list_tools())
        try:
            async with httpx.AsyncClient() as client:
                response = await asyncio.wait_for(client.post(url+'/call-tool',
                    headers={'Authorization':'Bearer '+token},
                    json={'tool_name':'playwright_with_chunk_browser_evaluate','tool_args':{}}),2)
            assert response.status_code == 200
            result = response.json()
            assert result['isError'] is True
            assert result['_meta']['mcphub/taskException']['kind'] == 'mcp'
            assert 'timed out' in result['content'][0]['text'].lower()
        finally:
            await bridge.unbind(binding)
