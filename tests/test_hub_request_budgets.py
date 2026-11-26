"""Execution budgets survive SDK mapping while outer waits include Hub admission."""
import asyncio
import json
import sys
from pathlib import Path
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_timeouts import upstream_seconds, upstream_timeout_headers, hub_requests_timeout

@pytest.mark.parametrize('value,seconds', [(10,10), ((2,50),50), ({'connect':1,'read':70},70), (httpx.Timeout(80),80), (None,300), ((2,None),300)])
def test_timeout_forms_retain_read_budget(value, seconds):
    assert upstream_seconds(value)==seconds
    assert upstream_timeout_headers(value)=={'X-MCPHub-Upstream-Timeout-Ms':str(seconds*1000)}
    assert hub_requests_timeout(value)==(15,seconds+615)

@pytest.mark.parametrize('value', [0,-1,float('inf'),float('nan')])
def test_invalid_timeouts_do_not_silently_disable_protection(value):
    with pytest.raises(ValueError):upstream_seconds(value)


def test_httpx_and_requests_relay_preserve_effective_read_budgets(monkeypatch):
    from utils.mcp.relay_adapter import arxiv_mcp_compat as adapter
    from utils.mcp.relay_adapter.egress_relay_client import RelayResponse
    calls=[]
    monkeypatch.setattr(adapter,'enabled',lambda:True)
    def fetch(method,url,**kwargs):
        calls.append(kwargs)
        return RelayResponse(200,{},b'original',url)
    monkeypatch.setattr(adapter,'fetch',fetch)
    async def fetch_async(*args,**kwargs):return fetch(*args,**kwargs)
    monkeypatch.setattr(adapter,'fetch_async',fetch_async)
    # Undo the installers after this test; do not leak global client wrappers.
    monkeypatch.setattr(httpx.Client,'request',httpx.Client.request)
    monkeypatch.setattr(httpx.AsyncClient,'request',httpx.AsyncClient.request)
    monkeypatch.setattr(httpx.Client,'stream',httpx.Client.stream)
    import requests
    monkeypatch.setattr(requests.Session,'request',requests.Session.request)
    adapter.install_httpx_relay();adapter.install_requests_relay()
    with httpx.Client(timeout=httpx.Timeout(240,connect=2)) as client:
        assert client.get('https://export.arxiv.org/api/query').content==b'original'
        client.get('https://export.arxiv.org/api/query',timeout=10)
    with requests.Session() as client:
        client.get('https://export.arxiv.org/api/query',timeout=(1,25))
    async def run():
        async with httpx.AsyncClient(timeout=75) as client:
            assert (await client.get('https://export.arxiv.org/api/query')).content==b'original'
    asyncio.run(run())
    assert [c['network_timeout'] for c in calls]==[240,10,25,75]


def test_relay_separates_network_budget_from_queue_and_legacy_fallback(monkeypatch):
    from utils.mcp.relay_adapter import egress_relay_client as relay
    monkeypatch.setattr(relay,'RELAY_URL','http://relay.test')
    monkeypatch.setattr(relay,'RELAY_TOKEN','test-only')
    monkeypatch.setattr(relay,'RELAY_TIMEOUT',90)
    monkeypatch.setenv('MCPHUB_QUEUE_WAIT_SECONDS','600')
    payload=json.loads(relay._request('GET','https://export.arxiv.org/api/query',network_timeout=10).data)
    assert payload['network_timeout_seconds']==10
    assert payload['timeout_seconds']==600
    legacy=json.loads(relay._request('GET','https://export.arxiv.org/api/query').data)
    assert legacy['timeout_seconds']==85
    assert 'network_timeout_seconds' not in legacy

@pytest.mark.asyncio
async def test_task_discovery_keeps_native_budget_instead_of_control_15_seconds(monkeypatch):
    from types import SimpleNamespace
    from agents.mcp import MCPServer
    from mcp.types import Tool, CallToolResult
    import utils.mcp.hub_transport as transport
    monkeypatch.setenv('MCPHUB_CALLBACK_HOST', '127.0.0.1')
    monkeypatch.setattr(transport, 'HUB_CONTROL_TIMEOUT_SECONDS', .01)
    original_client = httpx.AsyncClient
    async def respond(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'success':True,'data':{'id':'one','token':'key','path':'/gateway/v1/tasks/one'}})
        return httpx.Response(200,json={'success':True})
    monkeypatch.setattr(transport.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    class Backend(MCPServer):
        params=SimpleNamespace(env={})
        @property
        def name(self):return 'fetch'
        async def connect(self):pass
        async def cleanup(self):pass
        async def __aenter__(self):
            await self.connect();return self
        async def __aexit__(self,*args):await self.cleanup()
        async def list_tools(self):
            await asyncio.sleep(.06)
            return [Tool(name='read',inputSchema={'type':'object'})]
        async def call_tool(self,tool_name,arguments):return CallToolResult(content=[])
    settings=transport.HubSettings('http://hub.test','key','toolathlon',frozenset(),'','-')
    bridge=transport.TaskBridge(settings)
    async with transport.HubTaskServer('fetch',Backend(),bridge,{'client_session_timeout_seconds':.2}) as server:
        assert server.connection['id']=='one'
    assert bridge.runner is None


def test_urllib3_total_timeout_does_not_require_a_started_connect_timer():
    from urllib3.util import Timeout
    assert upstream_seconds(Timeout(total=20)) == 20
    assert upstream_seconds(Timeout(connect=2, read=10)) == 10
