import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_github import GithubAPIProxy, HOST


@pytest.fixture
async def gateway(monkeypatch):
    monkeypatch.setenv('MCPHUB_RUN_ID','run-a')
    monkeypatch.setenv('MCPHUB_RUN_TOKEN','run-capability')
    monkeypatch.setenv('MCPHUB_TASK_ID','finalpool/example')
    calls=[]
    async def handle(request):
        binding=request.match_info['binding'];path=request.match_info['path']
        calls.append({'binding':binding,'path':path,'query':request.query_string,
                      'headers':dict(request.headers),'body':await request.read()})
        if binding=='raw':return web.Response(text='hello',content_type='text/plain')
        if path=='graphql':return web.json_response({'data':{'viewer':{'login':'fixture'}}})
        if '/contents/' in path:return web.json_response({'type':'file','sha':'abc','size':5,'name':'README.md','path':'README.md'})
        return web.json_response({'login':'fixture','id':123,'permissions':{'pull':True,'push':True}})
    app=web.Application();app.router.add_route('*','/api/workspace-runs/run-a/http/github/{binding}/{path:.*}',handle)
    runner=web.AppRunner(app,access_log=None);await runner.setup();await web.TCPSite(runner,'127.0.0.1',0).start()
    settings=SimpleNamespace(root=f'http://127.0.0.1:{runner.addresses[0][1]}',headers={
        'Authorization':'Bearer hub-group-key','X-MCPHub-Run-Token':'run-capability'})
    proxy=await GithubAPIProxy(settings,5).start()
    try:yield proxy,calls
    finally:await proxy.close();await runner.cleanup()


@pytest.mark.asyncio
async def test_proxy_is_authenticated_and_preserves_routes_and_payload(gateway):
    proxy,calls=gateway
    async with httpx.AsyncClient(trust_env=False,proxy=proxy.proxy_url) as client:
        assert (await client.get('http://'+HOST+'/api/v3/user')).status_code==403
        for path,binding in [('/api/v3/repos/a/b/contents/a%20b?ref=main','api'),('/api/graphql','api'),('/raw/a/b/main/README.md','raw'),('/api/uploads/repos/a/b/releases/1/assets?name=x','uploads')]:
            result=await client.post('http://'+HOST+path,content=b'original bytes',headers={'Authorization':'Bearer '+proxy.token})
            assert result.status_code==200
            assert calls[-1]['binding']==binding
            assert calls[-1]['body']==b'original bytes'
            assert calls[-1]['headers']['Authorization']=='Bearer hub-group-key'
            assert calls[-1]['headers']['X-MCPHub-Run-Token']=='run-capability'
            assert calls[-1]['headers']['X-MCPHub-Stage']=='tool'
        count=len(calls)
        assert (await client.get('http://untrusted.invalid/api/v3/user',headers={'Authorization':'Bearer '+proxy.token})).status_code==403
        assert len(calls)==count
    assert proxy.environment['GITHUB_PERSONAL_ACCESS_TOKEN']==proxy.token
    assert 'run-capability' not in repr(proxy.environment)


@pytest.mark.asyncio
async def test_original_go_binary_keeps_schema_and_uses_hub_without_real_credentials(gateway):
    binary=os.getenv('TOOLATHLON_GITHUB_TEST_BINARY')
    if not binary:pytest.skip('Set TOOLATHLON_GITHUB_TEST_BINARY to verify the pinned native binary')
    from agents.mcp import MCPServerStdio
    proxy,calls=gateway
    original=MCPServerStdio(params={'command':binary,'args':['stdio'],'env':{'GITHUB_PERSONAL_ACCESS_TOKEN':'fixture-only'}})
    async with original:
        direct=[tool.model_dump(by_alias=True,exclude_none=True) for tool in await original.list_tools()]
    server=MCPServerStdio(params={'command':binary,'args':['stdio'],'env':proxy.environment})
    async with server:
        forwarded=[tool.model_dump(by_alias=True,exclude_none=True) for tool in await server.list_tools()]
        assert forwarded==direct
        result=await server.call_tool('get_me',{})
        assert not result.isError
        assert any(call['binding']=='api' and call['path']=='user' for call in calls)
        result=await server.call_tool('get_file_contents',{'owner':'fixture','repo':'repo','path':'README.md','ref':'main'})
        assert not result.isError
        assert any(call['binding']=='raw' for call in calls)
    assert all(call['headers']['Authorization']=='Bearer hub-group-key' for call in calls)
