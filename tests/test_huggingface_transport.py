import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import sys

from aiohttp import web
from mcp.shared.exceptions import McpError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.huggingface_transport import FAILURE_MARKER, HuggingFaceMCPServer


@asynccontextmanager
async def endpoint(mode):
    calls, sessions = [], []

    async def handle(request):
        if request.method == 'GET':
            return web.Response(status=405)
        if request.method == 'DELETE':
            return web.Response(status=200)
        body = await request.json()
        method = body['method']
        if method.startswith('notifications/'):
            return web.Response(status=202)
        if method == 'initialize':
            session = f'session-{len(sessions)}'
            sessions.append(session)
            return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': {
                'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'test', 'version': '1'}}}, headers={'Mcp-Session-Id': session})
        if method == 'tools/list':
            result = {'tools': [{'name': 'read', 'description': 'read',
                                 'inputSchema': {'type': 'object'}}]}
        else:
            calls.append((request.headers.get('Mcp-Session-Id'), body['params']))
            if mode == 'always_lost' or mode == 'lost_once' and len(calls) == 1:
                return web.json_response({'error': 'Session not found'}, status=404)
            if mode == 'timeout':
                await asyncio.sleep(.3)
            if mode == 'argument_error':
                return web.json_response({'jsonrpc': '2.0', 'id': body['id'],
                                          'error': {'code': -32602, 'message': 'invalid arguments'}})
            result = {'content': [{'type': 'text', 'text': 'result'}], 'isError': mode == 'tool_error'}
        return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    app = web.Application()
    app.router.add_route('*', '/mcp', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    server = HuggingFaceMCPServer({'args': ['mcp-remote', 'https://huggingface.co/mcp',
                                          '--header', 'Authorization: Bearer test-key']},
                                {'client_session_timeout_seconds': 2})
    server.params['url'] = f'http://127.0.0.1:{runner.addresses[0][1]}/mcp'
    try:
        async with server:
            if mode == 'timeout':
                server.client.session._session_read_timeout_seconds = timedelta(seconds=.05)
            yield server, calls, sessions
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_session_404_reconnects_once_and_keeps_the_new_session(capsys):
    async with endpoint('lost_once') as (server, calls, sessions):
        assert (await server.list_tools())[0].name == 'read'
        assert server.params['headers']['Authorization'] == 'Bearer test-key'
        assert not (await server.call_tool('read', {'id': 'a'})).isError
        assert not (await server.call_tool('read', {'id': 'b'})).isError
        assert len(sessions) == 2 and len(calls) == 3
        assert calls[0][0] != calls[1][0] == calls[2][0]
        assert calls[0][1] == calls[1][1]
    assert server.owner is None and server.client is None
    assert FAILURE_MARKER not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_second_session_failure_is_reported_for_task_retry(capsys):
    async with endpoint('always_lost') as (server, calls, sessions):
        with pytest.raises(RuntimeError, match=FAILURE_MARKER):
            await asyncio.wait_for(server.call_tool('read', {}), 3)
        assert len(sessions) == len(calls) == 2
    assert FAILURE_MARKER in capsys.readouterr().out


@pytest.mark.asyncio
async def test_timeout_does_not_replay_a_possibly_executed_request():
    async with endpoint('timeout') as (server, calls, sessions):
        with pytest.raises(RuntimeError, match=FAILURE_MARKER):
            await asyncio.wait_for(server.call_tool('read', {}), 2)
        assert len(sessions) == len(calls) == 1


@pytest.mark.asyncio
async def test_argument_and_business_errors_do_not_trigger_reconnection(capsys):
    async with endpoint('argument_error') as (server, calls, sessions):
        with pytest.raises(McpError, match='invalid arguments'):
            await server.call_tool('read', {})
        assert len(sessions) == len(calls) == 1
    async with endpoint('tool_error') as (server, calls, sessions):
        assert (await server.call_tool('read', {})).isError
        assert len(sessions) == len(calls) == 1
    assert FAILURE_MARKER not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_concurrent_session_failures_share_one_reconnection():
    async with endpoint('always_lost') as (server, calls, sessions):
        outcomes = await asyncio.gather(server.call_tool('read', {'id': 'a'}),
                                        server.call_tool('read', {'id': 'b'}),
                                        return_exceptions=True)
        assert all(isinstance(error, RuntimeError) for error in outcomes)
        assert len(sessions) == 2 and len(calls) == 4


@pytest.mark.asyncio
async def test_cancellation_is_not_retried_and_connection_is_closed(capsys):
    async with endpoint('timeout') as (server, calls, sessions):
        server.client.session._session_read_timeout_seconds = timedelta(seconds=2)
        task = asyncio.create_task(server.call_tool('read', {}))
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(.001)
        assert calls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(sessions) == len(calls) == 1
    assert server.owner is None
    assert FAILURE_MARKER not in capsys.readouterr().out


async def _initialization_failure_probe(mode):
    """Use a process boundary because leaked AnyIO scopes can spin the event loop."""
    server = HuggingFaceMCPServer({'args': []}, {'client_session_timeout_seconds': .05})
    server.params['timeout'] = server.params['sse_read_timeout'] = timedelta(seconds=2)
    started = asyncio.Event()

    async def handle(request):
        started.set()
        if mode == 'disconnect':
            request.transport.close()
        elif mode in {'timeout', 'cancel', 'outer_timeout'}:
            await asyncio.sleep(.2)
        return web.Response(status=503)

    app = web.Application()
    app.router.add_route('*', '/mcp', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 0).start()
    server.params['url'] = f'http://127.0.0.1:{runner.addresses[0][1]}/mcp'
    try:
        if mode == 'cancel':
            task = asyncio.create_task(server.connect())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif mode == 'outer_timeout':
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.02):
                    await server.connect()
        else:
            for _ in range(2):
                with pytest.raises(Exception):
                    await server.connect()
        assert server.owner is None and server.client is None and server.stop is None
        await server.cleanup()
        # No transport owner or cancellation callback may survive a failed open.
        await asyncio.sleep(.01)
        assert not [t for t in asyncio.all_tasks()
                    if '_own_connection' in t.get_coro().__qualname__]
    finally:
        await runner.cleanup()


@pytest.mark.parametrize('mode', ['error', 'disconnect', 'timeout', 'cancel', 'outer_timeout'])
def test_initialization_failure_and_cancellation_release_transport_scopes(mode):
    import subprocess

    code = (f'import asyncio, runpy; scope = runpy.run_path({str(Path(__file__).resolve())!r}); '
            f'asyncio.run(scope["_initialization_failure_probe"]({mode!r}))')
    result = subprocess.run([sys.executable, '-P', '-c', code], capture_output=True,
                            text=True, timeout=8)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'closing of asynchronous generator' not in result.stderr
    assert 'different task than it was entered' not in result.stderr
    assert 'Task exception was never retrieved' not in result.stderr
