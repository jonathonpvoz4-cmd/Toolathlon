"""Exercise the actual manager with native MCP transport and isolated HTTP faults."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types

from aiohttp import web
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.huggingface_transport import HuggingFaceMCPServer


@pytest.fixture
def manager(tmp_path, monkeypatch):
    # No account credentials are needed for an empty MCP catalog.
    with monkeypatch.context() as imports:
        for name, field in [('configs.global_configs', 'global_configs'),
                            ('configs.token_key_session', 'all_token_key_session')]:
            module = types.ModuleType(name)
            setattr(module, field, {})
            imports.setitem(sys.modules, name, module)
        path = Path(__file__).resolve().parents[1] / 'utils/mcp/tool_servers.py'
        spec = importlib.util.spec_from_file_location('lifecycle_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.MCPServerManager(str(tmp_path), config_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_native_http_429_during_session_lifetime_does_not_restart_on_close(manager, capsys):
    initializes = []

    async def handle(request):
        if request.method == 'GET':
            return web.Response(status=405)
        if request.method == 'DELETE':
            return web.Response(status=204)
        body = await request.json()
        method = body['method']
        if method.startswith('notifications/'):
            return web.Response(status=202)
        if method == 'initialize':
            initializes.append(body)
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'local-fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': []}
        else:
            return web.Response(status=429)
        return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': result},
                                 headers={'Mcp-Session-Id': 'local-session'})

    app = web.Application()
    app.router.add_route('*', '/mcp', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    server = HuggingFaceMCPServer({'args': []}, {'client_session_timeout_seconds': 2})
    server.params['url'] = f'http://127.0.0.1:{runner.addresses[0][1]}/mcp'
    manager.servers['huggingface'] = server
    call = None
    try:
        await manager.connect_servers(connect_retry_delay=.01)
        await server.list_tools()
        owner = server.owner
        call = asyncio.create_task(server.call_tool('test', {}))
        # HTTP 429 terminates the native SDK transport task group. Its error
        # must be consumed during close, without entering another initialize.
        done, _ = await asyncio.wait([owner], timeout=2)
        assert done
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        lifecycle = manager._server_tasks['huggingface']
        await asyncio.wait_for(manager.disconnect_servers(), .5)
        assert lifecycle.cancelled()
        assert len(initializes) == 1
        assert not manager.connected_servers and not manager._server_tasks
        assert not manager._connection_events
        assert server.owner is None
        assert 'not reconnecting' in capsys.readouterr().out
    finally:
        if call:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
        await manager.disconnect_servers()
        await runner.cleanup()


class Backend:
    def __init__(self, startup_failures=0, close_error=False, stall_start=False):
        self.enters = self.exits = 0
        self.startup_failures = startup_failures
        self.close_error = close_error
        self.stall_start = stall_start
        self.started = asyncio.Event()

    async def __aenter__(self):
        self.enters += 1
        self.started.set()
        if self.stall_start:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ExceptionGroup('cancelled initialization', [RuntimeError('transport close')])
        if self.enters <= self.startup_failures:
            raise RuntimeError('startup failed')
        return self

    async def __aexit__(self, *_):
        self.exits += 1
        if self.close_error:
            raise ExceptionGroup('transport close', [RuntimeError('session failed')])


@pytest.mark.asyncio
@pytest.mark.parametrize('close_error', [False, True])
async def test_startup_retries_are_retained_but_shutdown_never_retries(manager, close_error):
    backend = Backend(startup_failures=2, close_error=close_error)
    manager.servers['test'] = backend
    await manager.connect_servers(connect_retry_delay=.001)
    assert backend.enters == 3
    lifecycle = manager._server_tasks['test']
    await asyncio.wait_for(manager.disconnect_servers(), .5)
    assert lifecycle.cancelled()
    assert backend.enters == 3 and backend.exits == 1
    assert not manager.connected_servers and not manager._server_tasks
    assert not manager._connection_events


@pytest.mark.asyncio
async def test_cancellation_replaced_by_initialization_exception_cannot_restart(manager):
    backend = Backend(stall_start=True)
    manager.servers['test'] = backend
    connect = asyncio.create_task(manager.connect_servers(connect_retry_delay=.001))
    await backend.started.wait()
    lifecycle = manager._server_tasks['test']
    lifecycle.cancel()
    with pytest.raises(ValueError, match='Only 0 servers connected'):
        await asyncio.wait_for(connect, .5)
    await asyncio.gather(lifecycle, return_exceptions=True)
    assert lifecycle.cancelled() and backend.enters == 1
    assert not manager._server_tasks and not manager._connection_events


@pytest.mark.asyncio
async def test_exhausted_startup_retries_release_connection_waiter(manager):
    backend = Backend(startup_failures=10)
    manager.servers['test'] = backend
    with pytest.raises(ValueError, match='Only 0 servers connected'):
        await manager.connect_servers(max_connect_retries=2, connect_retry_delay=.001)
    assert backend.enters == 3 and backend.exits == 0
    assert not manager.connected_servers and not manager._server_tasks
    assert not manager._connection_events
