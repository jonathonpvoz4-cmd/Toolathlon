import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from agents.mcp import MCPServer
from mcp.types import Tool, CallToolResult, TextContent
from mcp.types import ErrorData
from mcp.shared.exceptions import McpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_transport import HubSettings, HubTaskServer, TaskBridge, native_timeout


class Backend(MCPServer):
    def __init__(self, value):
        self.value = value
        self.open = False
        self.arguments = None

    @property
    def name(self):
        return 'notion'

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.cleanup()

    async def connect(self):
        self.open = True

    async def cleanup(self):
        self.open = False

    async def list_tools(self):
        return [Tool(name='read_page', description='original', inputSchema={'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']})]

    async def call_tool(self, tool_name, arguments):
        self.arguments = arguments
        return CallToolResult(content=[TextContent(type='text', text=self.value)], isError=False)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'hub')
    monkeypatch.setenv('MCPHUB_URL', 'http://192.168.0.22:3003')
    monkeypatch.setenv('MCPHUB_TOKEN', 'test-group-key')
    monkeypatch.setenv('MCPHUB_CALLBACK_HOST', '127.0.0.1')
    return HubSettings.from_environment()


def test_config_and_native_deadlines(settings, monkeypatch):
    assert settings.mcp_url('google_map') == 'http://192.168.0.22:3003/mcp/toolathlon/google_map'
    assert native_timeout({}) == 5
    assert native_timeout({'client_session_timeout_seconds': 1000}) == 1000
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    assert HubSettings.from_environment() is None
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'hub')
    monkeypatch.setenv('MCPHUB_TOKEN', '')
    with pytest.raises(ValueError):
        HubSettings.from_environment()


@pytest.mark.asyncio
async def test_callback_isolation_schema_and_revocation(settings):
    bridges = [TaskBridge(settings), TaskBridge(settings)]
    assert bridges[0].run_id != bridges[1].run_id
    bindings = []
    try:
        async with httpx.AsyncClient() as http:
            for i, bridge in enumerate(bridges):
                backend = Backend(str(i))
                binding, token, url = await bridge.bind('notion', backend, await backend.list_tools())
                bindings.append((binding, token, url))
            for i, (binding, token, url) in enumerate(bindings):
                headers = {'Authorization': 'Bearer ' + token}
                assert (await http.post(url + '/list-tools')).status_code == 403
                assert (await http.post(url + '/list-tools', headers={'Authorization': 'Bearer ' + bindings[1-i][1]})).status_code == 403
                tools = (await http.post(url + '/list-tools', headers=headers)).json()
                original = (await Backend('').list_tools())[0].model_dump(by_alias=True, exclude_none=True)
                assert tools == [{**original, 'name': 'notion_read_page'}]
                result = await http.post(url + '/call-tool', headers=headers, json={'tool_name': 'notion_read_page', 'tool_args': {'id': 'original'}})
                assert result.json()['content'][0]['text'] == str(i)
                assert (await http.post(url + '/call-tool', headers=headers, json={'tool_name': 'other', 'tool_args': {}})).status_code == 400
                await bridges[i].unbind(binding)
                assert not bridges[i].bindings
    finally:
        for bridge, item in zip(bridges, bindings):
            await bridge.unbind(item[0])


@pytest.mark.asyncio
async def test_task_wrapper_round_trip_and_cleanup(settings, monkeypatch):
    leases = {}
    async def register(request):
        assert request.headers['Authorization'] == 'Bearer test-group-key'
        body = await request.json()
        assert body['catalog'] == 'toolathlon'
        leases['one'] = body
        return web.json_response({'success': True, 'data': {'id': 'one', 'token': 'lease-key', 'path': '/gateway/v1/tasks/one'}})
    async def dispatch(request):
        assert request.headers['Authorization'] == 'Bearer lease-key'
        body = leases['one']
        async with httpx.AsyncClient() as http:
            response = await http.post(body['url'] + '/' + request.match_info['operation'], headers={'Authorization': 'Bearer ' + body['token']}, json=await request.json())
        return web.json_response(response.json(), status=response.status_code)
    async def remove(request):
        leases.pop('one')
        return web.json_response({'success': True})
    app = web.Application()
    app.router.add_post('/api/task-connections', register)
    app.router.add_delete('/api/task-connections/one', remove)
    app.router.add_post('/gateway/v1/tasks/one/{operation}', dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    from dataclasses import replace
    config = replace(settings, root=f'http://127.0.0.1:{runner.addresses[0][1]}')
    backend = Backend('task-a')
    bridge = TaskBridge(config)
    server = HubTaskServer('notion', backend, bridge, {'client_session_timeout_seconds': 100})
    try:
        async with server:
            assert backend.open
            assert await server.list_tools() == await backend.list_tools()
            assert (await server.call_tool('read_page', {'id': 'a'})).content[0].text == 'task-a'
            assert backend.arguments == {'id': 'a'}
            error = ErrorData(code=-32603, message='No output', data={'reason': 'empty'})
            async def mcp_failure(*args, **kwargs):
                raise McpError(error)
            monkeypatch.setattr(backend, 'call_tool', mcp_failure)
            with pytest.raises(McpError) as raised:
                await server.call_tool('read_page', {})
            assert raised.value.error == error
            async def parameter_failure(*args, **kwargs):
                raise RuntimeError('unable to match a printer: custom-columns')
            monkeypatch.setattr(backend, 'call_tool', parameter_failure)
            with pytest.raises(RuntimeError, match='unable to match a printer: custom-columns'):
                await server.call_tool('read_page', {})
            async def empty_timeout(*args, **kwargs):
                raise TimeoutError()
            monkeypatch.setattr(backend, 'call_tool', empty_timeout)
            with pytest.raises(RuntimeError, match='^TimeoutError$'):
                await server.call_tool('read_page', {})
            original_error_result = CallToolResult(
                content=[TextContent(type='text', text='ordinary tool error')], isError=True,
                _meta={'provider': 'original'},
            )
            async def tool_failure(*args, **kwargs):
                return original_error_result
            monkeypatch.setattr(backend, 'call_tool', tool_failure)
            assert await server.call_tool('read_page', {}) == original_error_result
        assert not leases and not backend.open and bridge.runner is None
        with pytest.raises(RuntimeError):
            await server.call_tool('read_page', {})
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout_type', [httpx.ReadTimeout, httpx.ConnectTimeout,
                                         httpx.WriteTimeout, httpx.PoolTimeout])
async def test_task_http_timeout_is_explicit_private_and_not_retried(settings, timeout_type):
    calls = []
    async def fail(request):
        calls.append(request)
        raise timeout_type('sensitive-upstream-value', request=request)
    server = HubTaskServer('google_sheet', Backend('unused'), TaskBridge(settings),
                           {'client_session_timeout_seconds': 60})
    server.failed = False
    server.connection = {'path': '/gateway/v1/tasks/private-lease', 'token': 'private-capability'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        server.http = client
        with pytest.raises(RuntimeError) as raised:
            await server._request('call-tool', {'tool_args': {'secret': 'private-argument'}})
    message = str(raised.value)
    assert 'google_sheet' in message and 'call-tool' in message
    assert timeout_type.__name__ in message and '60s' in message
    assert len(calls) == 1
    for secret in ['private-lease', 'private-capability', 'private-argument',
                   'sensitive-upstream-value', settings.token, settings.root]:
        assert secret not in message


def test_serper_full_url_and_separate_group_key(monkeypatch):
    from utils.aux_tools.serper_endpoint import resolve_serper_endpoint
    monkeypatch.delenv('SERPER_API_URL', raising=False)
    assert resolve_serper_endpoint('https://direct/serper/search', 'upstream-secret') == ('https://direct/serper/search', 'upstream-secret')
    monkeypatch.setenv('SERPER_API_URL', 'http://192.168.0.22:3003/http/serper/search')
    monkeypatch.setenv('MCPHUB_TOKEN', 'toolathlon-group')
    monkeypatch.delenv('SERPER_API_KEY', raising=False)
    assert resolve_serper_endpoint('unused', 'must-never-forward')[1] == 'toolathlon-group'
    monkeypatch.setenv('SERPER_API_KEY', 'public-search-group')
    assert resolve_serper_endpoint('unused', 'must-never-forward') == ('http://192.168.0.22:3003/http/serper/search', 'public-search-group')
    monkeypatch.delenv('SERPER_API_KEY')
    monkeypatch.delenv('MCPHUB_TOKEN')
    with pytest.raises(ValueError):
        resolve_serper_endpoint('unused', 'must-never-forward')


@pytest.mark.asyncio
async def test_shared_tools_strip_only_one_hub_prefix(settings, monkeypatch):
    from utils.mcp.hub_transport import HubSharedServer
    from agents.mcp import MCPServerStreamableHttp
    original = Tool(name='fetch_get', inputSchema={'type':'object','properties':{'url':{'type':'string'}}})
    async def inventory(self):
        return [original]
    seen = []
    async def call(self, name, arguments):
        seen.append((name,arguments))
        return CallToolResult(content=[TextContent(type='text',text='ok')])
    monkeypatch.setattr(MCPServerStreamableHttp, 'list_tools', inventory)
    monkeypatch.setattr(MCPServerStreamableHttp, 'call_tool', call)
    # Test against an alternate configured separator too.
    from dataclasses import replace
    server = HubSharedServer('fetch', replace(settings,separator='_'), {'client_session_timeout_seconds':100})
    tools = await server.list_tools()
    assert tools[0].name == 'get'
    assert tools[0].inputSchema == original.inputSchema
    await server.call_tool('get', {'url':'unchanged'})
    assert seen == [('fetch_get', {'url':'unchanged'})]


def test_manager_keeps_preparation_direct_and_shared_credentials_in_hub(settings, monkeypatch, tmp_path):
    import importlib
    import types
    config = types.ModuleType('configs.global_configs')
    config.global_configs = type('Config',(dict,),{'podman_or_docker':'docker'})()
    tokens = types.ModuleType('configs.token_key_session')
    tokens.all_token_key_session = {}
    monkeypatch.setitem(sys.modules,'configs.global_configs',config)
    monkeypatch.setitem(sys.modules,'configs.token_key_session',tokens)
    module = importlib.import_module('utils.mcp.tool_servers')
    (tmp_path/'fetch.yaml').write_text('name: fetch\nparams:\n  command: npx\n  args: [fetch]\nclient_session_timeout_seconds: 100\n')
    direct = module.MCPServerManager('/tmp/workspace',str(tmp_path))
    assert direct.hub_settings is None
    assert isinstance(direct.servers['fetch'],module.MCPServerStdio)
    def never_expand(self,*args):
        raise AssertionError('shared credentials must remain in Hub')
    monkeypatch.setattr(module.MCPServerManager,'_process_config_params',never_expand)
    routed = module.MCPServerManager('/tmp/workspace',str(tmp_path),use_hub=True)
    assert isinstance(routed.servers['fetch'],module.HubSharedServer)
    (tmp_path/'other.yaml').write_text('name: notion\nparams:\n  command: invalid\n')
    with pytest.raises(ValueError):
        module.MCPServerManager('/tmp/workspace',str(tmp_path),use_hub=True)
    sys.modules.pop('utils.mcp.tool_servers', None)


def test_n_run_preserves_routing_through_sudo_without_logging_secret(monkeypatch):
    import importlib.util
    path = Path(__file__).resolve().parents[1]/'scripts/n_run.py'
    spec = importlib.util.spec_from_file_location('hub_test_n_run',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module,'_as_root',lambda:False)
    monkeypatch.setenv('MCPHUB_TOKEN','secret-must-not-appear-in-command')
    monkeypatch.setenv('SERPER_API_URL','http://192.168.0.22:3003/http/serper/search')
    command = module._command(module.RUN_SCRIPT,'model',needs_sudo=True)
    assert command[0]=='sudo'
    assert 'MCPHUB_TOKEN' in command[1] and 'SERPER_API_URL' in command[1]
    assert 'secret-must-not-appear-in-command' not in ' '.join(command)
    assert module._command(module.DEPLOY_SCRIPT,'true',needs_sudo=True) == ['sudo','bash',module.DEPLOY_SCRIPT,'true']


def test_hub_search_derives_url_and_never_falls_back_to_source_key(monkeypatch):
    from utils.aux_tools.serper_endpoint import resolve_serper_endpoint
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'hub')
    monkeypatch.setenv('MCPHUB_URL', 'http://192.168.0.22:3003/base/')
    monkeypatch.setenv('MCPHUB_TOKEN', 'group-key')
    monkeypatch.delenv('SERPER_API_URL', raising=False)
    monkeypatch.delenv('SERPER_API_KEY', raising=False)
    assert resolve_serper_endpoint('https://provider.example/search', 'source-provider-key') == (
        'http://192.168.0.22:3003/base/http/serper/search', 'group-key')
    monkeypatch.delenv('MCPHUB_TOKEN')
    with pytest.raises(ValueError):
        resolve_serper_endpoint('https://provider.example/search', 'source-provider-key')
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    assert resolve_serper_endpoint('https://provider.example/search', 'source-provider-key') == (
        'https://provider.example/search', 'source-provider-key')
