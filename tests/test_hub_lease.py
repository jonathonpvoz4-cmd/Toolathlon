"""Lease recovery must preserve business calls and the task retry boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from test_hub_transport import Backend, settings
from utils.mcp import hub_transport
from utils.mcp.hub_transport import HubTaskServer, TaskBridge
from utils.mcp.task_transport_error import FAILURE_MARKER, HubTaskTransportError


def lease(settings, monkeypatch):
    clock = [0.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(hub_transport, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(hub_transport, 'sleep', sleep)
    monkeypatch.setattr(hub_transport.random, 'uniform', lambda *_: 1.0)
    server = HubTaskServer('notion', Backend('ok'), TaskBridge(settings), {})
    server.connection = {'id': 'lease', 'token': 'private-lease-token', 'path': '/gateway/v1/tasks/lease'}
    server.failed = False
    server.lease_deadline = 300.0
    server.binding = 'live-callback'
    server.bridge.unbind = AsyncMock()
    return server, clock, sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', '503', '429', 'invalid-json', 'invalid-shape'])
async def test_transient_renewal_recovers_without_disabling_or_replaying_tools(settings, monkeypatch, caplog, failure):
    server, clock, sleeps = lease(settings, monkeypatch)
    puts, posts = [], []

    async def request(req):
        if req.method == 'POST':
            posts.append(req)
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'ok'}]})
        puts.append(req)
        if len(puts) == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('private-exception-text')
            if failure == 'invalid-json':
                return httpx.Response(200, text='broken')
            if failure == 'invalid-shape':
                return httpx.Response(200, json=[])
            return httpx.Response(int(failure), headers={'Retry-After': '7'})
        # A live tool is still usable while renewal is recovering.
        assert (await server._request('call-tool', {}))['content'][0]['text'] == 'ok'
        assert not server.failed and server.binding == 'live-callback'
        return httpx.Response(200, json={'success': True})

    fake_sleep = hub_transport.sleep

    async def sleep(delay):
        if len(puts) == 2:
            raise asyncio.CancelledError
        await fake_sleep(delay)

    monkeypatch.setattr(hub_transport, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        server.http = client
        with pytest.raises(asyncio.CancelledError):
            await server._renew()
    assert len(puts) == 2 and len(posts) == 1
    assert server.lease_deadline == clock[0] + 300
    server.bridge.unbind.assert_not_called()
    if failure in {'503', '429'}:
        assert sleeps[1] >= 7
    assert not server.failed
    for secret in [settings.token, settings.root, 'private-exception-text', 'private-lease-token']:
        assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 404, 400])
async def test_revoked_lease_stops_without_reregistering_or_retrying_tools(settings, monkeypatch, status):
    server, _, _ = lease(settings, monkeypatch)
    calls = []

    async def request(req):
        calls.append(req)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        server.http = client
        await server._renew()
        with pytest.raises(HubTaskTransportError, match=FAILURE_MARKER):
            await server._request('call-tool', {})
        with pytest.raises(HubTaskTransportError):
            await server.list_tools()
    assert len(calls) == 1 and calls[0].method == 'PUT'
    # Cleanup owns active callbacks, so lease failure cannot cancel an unrelated in-flight call.
    server.bridge.unbind.assert_not_called()


@pytest.mark.asyncio
async def test_failed_renewals_cannot_extend_the_confirmed_lease(settings, monkeypatch):
    server, clock, _ = lease(settings, monkeypatch)
    calls = []

    async def request(req):
        calls.append(req)
        clock[0] += min(15, server.lease_deadline - clock[0])
        raise httpx.ReadTimeout('temporary')

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        server.http = client
        await server._renew()
    assert 1 < len(calls) < 20
    assert server.failed and clock[0] == server.lease_deadline == 300


@pytest.mark.asyncio
async def test_cancel_renewal_does_not_mark_a_live_lease_failed(settings, monkeypatch):
    server, _, _ = lease(settings, monkeypatch)

    async def sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(hub_transport, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await server._renew()
    assert not server.failed


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['Task connection unavailable', 'business permission denied'])
async def test_gateway_revocation_is_distinguished_from_business_403(settings, monkeypatch, message):
    server, _, _ = lease(settings, monkeypatch)
    calls = []

    async def request(req):
        calls.append(req)
        return httpx.Response(403, json={'message': message})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        server.http = client
        with pytest.raises(RuntimeError) as raised:
            await server._request('call-tool', {})
    revoked = message == 'Task connection unavailable'
    assert isinstance(raised.value, HubTaskTransportError) is revoked
    assert server.failed is revoked
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('infrastructure', [True, False])
async def test_agent_tool_boundary_propagates_only_terminal_hub_failure(infrastructure):
    from agents import Agent, RunConfig, RunContextWrapper, RunHooks
    from agents._run_impl import ResponseFunctionToolCall, RunImpl
    from agents.mcp.util import MCPUtil
    from mcp.types import Tool
    from utils.openai_agents_monkey_patch import custom_mcp_util, custom_run_impl

    class Server:
        name = 'notion'

        async def call_tool(self, *_):
            if infrastructure:
                raise HubTaskTransportError(FAILURE_MARKER)
            raise ValueError('invalid arguments')

    tool = MCPUtil.to_function_tool(Tool(name='read', inputSchema={'type': 'object'}), Server(), False)
    kwargs = dict(
        agent=Agent(name='test', tools=[tool]), hooks=RunHooks(),
        context_wrapper=RunContextWrapper(context={}), config=RunConfig(),
        tool_runs=[SimpleNamespace(function_tool=tool, tool_call=ResponseFunctionToolCall(
            arguments='{}', call_id='one', name=tool.name, type='function_call'))],
    )
    if infrastructure:
        with pytest.raises(HubTaskTransportError):
            await RunImpl.execute_function_tool_calls(**kwargs)
    else:
        results = await RunImpl.execute_function_tool_calls(**kwargs)
        assert 'invalid arguments' in results[0].output
