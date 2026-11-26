"""Native Hugging Face MCP sessions with one recovery for an explicit session loss."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import math

from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp.shared.exceptions import McpError


FAILURE_MARKER = 'HUGGINGFACE_MCP_TRANSPORT_FAILURE'


def session_lost(error):
    # MCP 1.9 converts an HTTP endpoint 404 to this specific protocol error.
    return (isinstance(error, McpError) and error.error.code == 32600
            and error.error.message == 'Session terminated')


def response_timed_out(error):
    return (isinstance(error, McpError) and error.error.code == 408
            and error.error.message.startswith('Timed out while waiting for response to '))


class _Connection(MCPServerStreamableHttp):
    async def cleanup(self):
        # Retain transport errors: the SDK otherwise logs and swallows the
        # ExceptionGroup that explains an internally cancelled initialization.
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
            finally:
                self.session = None


class HuggingFaceMCPServer(MCPServer):
    def __init__(self, params, config):
        args = params.get('args', [])
        headers = {}
        for index, arg in enumerate(args[:-1]):
            if arg == '--header':
                name, separator, value = args[index + 1].partition(':')
                if separator:
                    headers[name.strip()] = value.strip()
        timeout = float(config.get('client_session_timeout_seconds') or 120)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Hugging Face MCP timeout must be positive and finite')
        self.params = {'url': 'https://huggingface.co/mcp', 'headers': headers,
                       'timeout': timedelta(seconds=timeout),
                       'sse_read_timeout': timedelta(seconds=timeout)}
        self.client_session_timeout_seconds = timeout
        self.cache_tools_list = config.get('cache_tools_list', True)
        self.client = None
        self.owner = None
        self.stop = None
        self.lock = asyncio.Lock()
        self.pending = {}

    @property
    def name(self):
        return 'huggingface'

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.cleanup()

    async def _own_connection(self, client, ready, stop):
        # The same task must enter/exit the SDK's AnyIO transport scopes.
        try:
            try:
                await client.connect()
                ready.set_result(client)
                await stop.wait()
            finally:
                # SDK __aenter__ may be cancelled after opening transport scopes.
                # __aexit__ is not called when __aenter__ fails.
                await client.cleanup()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    async def _close(self):
        if self.owner is not None:
            self.stop.set()
            print('[huggingface] Closing MCP session', flush=True)
            try:
                await self.owner
            finally:
                self.owner = self.client = self.stop = None
                print('[huggingface] MCP session owner exited', flush=True)

    async def _open(self):
        client = _Connection(
            name=self.name, params=self.params, cache_tools_list=self.cache_tools_list,
            client_session_timeout_seconds=self.client_session_timeout_seconds)
        ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.owner = asyncio.create_task(self._own_connection(client, ready, self.stop))
        try:
            self.client = await asyncio.shield(ready)
        except BaseException:
            ready.cancel()
            self.owner.cancel()
            try:
                await self._close()
            except BaseException:
                pass
            raise

    async def connect(self):
        async with self.lock:
            if self.client is None:
                await self._open()

    async def cleanup(self):
        async with self.lock:
            await self._close()

    async def list_tools(self):
        return await self.client.list_tools()

    async def _reconnect(self, previous):
        async with self.lock:
            if self.client is previous:
                active = self.pending.get(previous)
                if active:
                    await active[1].wait()
                await self._close()
                await self._open()

    @asynccontextmanager
    async def _use_connection(self):
        async with self.lock:
            client = self.client
            active = self.pending.setdefault(client, [0, asyncio.Event()])
            active[0] += 1
        try:
            yield client
        finally:
            active[0] -= 1
            if active[0] == 0:
                active[1].set()
                self.pending.pop(client, None)

    async def call_tool(self, tool_name, arguments):
        try:
            async with self._use_connection() as previous:
                return await previous.call_tool(tool_name, arguments)
        except McpError as error:
            if not session_lost(error):
                if response_timed_out(error):
                    print(FAILURE_MARKER, flush=True)
                    raise RuntimeError(FAILURE_MARKER) from None
                raise
        print('[huggingface] MCP session lost; reconnecting and retrying tool once', flush=True)
        try:
            await self._reconnect(previous)
        except Exception:
            print(FAILURE_MARKER, flush=True)
            raise RuntimeError(FAILURE_MARKER) from None
        try:
            async with self._use_connection() as client:
                return await client.call_tool(tool_name, arguments)
        except McpError as error:
            if session_lost(error) or response_timed_out(error):
                print(FAILURE_MARKER, flush=True)
                raise RuntimeError(FAILURE_MARKER) from None
            raise
