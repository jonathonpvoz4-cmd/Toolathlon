"""Optional MCPHub v2 transports; model aliases and task backends stay Toolathlon-owned."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import math
import os
import re
import secrets
import socket
import uuid
from pathlib import Path
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from urllib.parse import quote, urlsplit

import httpx
from aiohttp import web
from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp.types import CallToolResult, Tool

SHARED_SERVERS = frozenset({
    "rail_12306", "fetch", "google_map", "howtocook", "scholarly",
    "yahoo-finance", "youtube", "youtube-transcript",
})


@dataclass(frozen=True)
class HubSettings:
    root: str
    token: str
    catalog: str
    shared: frozenset[str]
    target: str
    separator: str

    @classmethod
    def from_environment(cls) -> "HubSettings | None":
        mode = os.getenv("MCP_TOOL_TRANSPORT", "direct").strip().lower()
        if mode == "direct":
            return None
        if mode != "hub":
            raise ValueError("MCP_TOOL_TRANSPORT must be direct or hub")
        root = os.getenv("MCPHUB_URL", "").strip().rstrip("/")
        url = urlsplit(root)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("MCPHUB_URL must be an HTTP(S) Hub root")
        token = os.getenv("MCPHUB_TOKEN", "").strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("MCPHUB_TOKEN must be a Hub calling key")
        catalog = os.getenv("MCPHUB_CATALOG", "toolathlon").strip()
        if catalog and not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", catalog):
            raise ValueError("Invalid MCPHUB_CATALOG")
        selected = os.getenv("MCPHUB_SERVERS")
        shared = SHARED_SERVERS if selected is None else frozenset(n.strip() for n in selected.split(",") if n.strip())
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in shared):
            raise ValueError("Invalid MCPHUB_SERVERS")
        target = os.getenv("MCPHUB_TARGET", "").strip()
        if len(target) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in target):
            raise ValueError("Invalid MCPHUB_TARGET")
        return cls(root, token, catalog, shared, target, os.getenv("MCPHUB_NAME_SEPARATOR", "-"))

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if os.getenv("MCPHUB_RUN_TOKEN"):
            headers["X-MCPHub-Run-Token"] = os.environ["MCPHUB_RUN_TOKEN"]
        return headers

    def mcp_url(self, name: str) -> str:
        space = self.catalog + "/" if self.catalog else ""
        return f"{self.root}/mcp/{space}{quote(name, safe='')}"


def native_timeout(config: dict[str, Any]) -> float:
    # This is the default in Toolathlon's pinned OpenAI Agents SDK 0.0.15.
    timeout = float(config.get("client_session_timeout_seconds") or 5)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP client timeout must be positive and finite")
    return timeout


class HubSharedServer(MCPServerStreamableHttp):
    def __init__(self, name: str, settings: HubSettings, config: dict[str, Any]):
        headers = settings.headers
        if settings.target:
            headers["X-MCPHub-Target"] = settings.target
        timeout = native_timeout(config)
        super().__init__(
            name=name,
            params={"url": settings.mcp_url(name), "headers": headers,
                    "sse_read_timeout": timedelta(seconds=timeout)},
            cache_tools_list=config.get("cache_tools_list", True),
            client_session_timeout_seconds=timeout,
        )
        self._hub_prefix = name + settings.separator

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        if any(not t.name.startswith(self._hub_prefix) for t in tools):
            raise RuntimeError(f"Hub tool prefix mismatch for {self.name}")
        # The existing harness adds server_name exactly once after this step.
        return [t.model_copy(update={"name": t.name[len(self._hub_prefix):]}) for t in tools]

    async def call_tool(self, tool_name: str, arguments: dict | None) -> CallToolResult:
        return await super().call_tool(self._hub_prefix + tool_name, arguments)


class TaskBridge:
    """One authenticated callback listener per manager/run, with per-server bindings."""
    def __init__(self, settings: HubSettings):
        self.settings = settings
        self.run_id = uuid.uuid4().hex
        self.bindings: dict[str, tuple[str, MCPServer, dict[str, Tool]]] = {}
        self.active: dict[str, set[asyncio.Task]] = {}
        self.runner: web.AppRunner | None = None
        self.root = ""
        self.lock = asyncio.Lock()

    async def bind(self, name: str, backend: MCPServer, tools: list[Tool]) -> tuple[str, str, str]:
        async with self.lock:
            if self.runner is None:
                app = web.Application(client_max_size=0)
                app.router.add_post('/bridge/{binding}/{operation}', self.handle)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                try:
                    site = web.TCPSite(runner, '0.0.0.0', 0)
                    await site.start()
                    host = os.getenv('MCPHUB_CALLBACK_HOST', '').strip()
                    if not host:
                        url = urlsplit(self.settings.root)
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                            route.connect((url.hostname, url.port or (443 if url.scheme == 'https' else 80)))
                            host = route.getsockname()[0]
                    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host):
                        raise ValueError('MCPHUB_CALLBACK_HOST must be a reachable hostname or IP')
                    if ':' in host:
                        host = '[' + host + ']'
                    self.root = f'http://{host}:{runner.addresses[0][1]}'
                    self.runner = runner
                except BaseException:
                    await runner.cleanup()
                    raise
            binding = uuid.uuid4().hex
            token = secrets.token_hex(32)
            self.bindings[binding] = (token, backend, {f'{name}_{t.name}': t for t in tools})
            return binding, token, f'{self.root}/bridge/{binding}'

    async def unbind(self, binding: str) -> None:
        async with self.lock:
            self.bindings.pop(binding, None)
            calls = self.active.pop(binding, set())
            for call in calls:
                call.cancel()
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
            if not self.bindings and self.runner:
                await self.runner.cleanup()
                self.runner = None

    async def handle(self, request: web.Request) -> web.Response:
        binding = request.match_info['binding']
        entry = self.bindings.get(binding)
        if not entry or not hmac.compare_digest(request.headers.get('Authorization', '').encode(), ('Bearer ' + entry[0]).encode()):
            raise web.HTTPForbidden()
        _, backend, tools = entry
        operation = request.match_info['operation']
        if operation == 'list-tools':
            return web.json_response([t.model_copy(update={'name': name}).model_dump(by_alias=True, exclude_none=True) for name, t in tools.items()])
        if operation != 'call-tool':
            raise web.HTTPNotFound()
        body = await request.json()
        if (not isinstance(body, dict) or body.get('tool_name') not in tools
                or not isinstance(body.get('tool_args'), dict)):
            raise web.HTTPBadRequest()
        task = asyncio.create_task(backend.call_tool(tools[body['tool_name']].name, body['tool_args']))
        self.active.setdefault(binding, set()).add(task)

        async def watch():
            while not task.done():
                if binding not in self.bindings or request.transport is None or request.transport.is_closing():
                    task.cancel()
                    return
                await asyncio.sleep(.25)

        watcher = asyncio.create_task(watch())
        try:
            try:
                result = await task
            except Exception as error:
                # Preserve direct-mode exception semantics through the audited tool result.
                # Cancellation is a BaseException and must still propagate unchanged.
                message = str(error) or type(error).__name__
                detail = ({'kind': 'mcp', 'error': error.error.model_dump(exclude_none=True)}
                          if isinstance(error, McpError)
                          else {'kind': 'exception', 'message': message})
                return web.json_response({
                    'content': [{'type': 'text', 'text': message}], 'isError': True,
                    '_meta': {'mcphub/taskException': detail},
                })
            return web.json_response(result.model_dump(by_alias=True, exclude_none=True))
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            self.active.get(binding, set()).discard(task)


class HubTaskServer(MCPServer):
    def __init__(self, name: str, backend: MCPServer, bridge: TaskBridge, config: dict[str, Any]):
        from utils.mcp.hub_stdio import install_linear_stdio_reader
        install_linear_stdio_reader()
        self._name = name
        self.backend = backend
        self.bridge = bridge
        self.timeout = native_timeout(config)
        self.settings = bridge.settings
        self.http: httpx.AsyncClient | None = None
        self.connection: dict | None = None
        self.binding = ''
        self.renewal: asyncio.Task | None = None
        self.entered = False
        self.failed = True
        self.cache_tools_list = config.get('cache_tools_list', True)
        self.tools: list[Tool] | None = None

    @property
    def name(self) -> str:
        return self._name

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.cleanup()

    async def connect(self):
        self.failed = False
        self.tools = None
        self.http = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        try:
            if self.name in {'arxiv_local', 'arxiv-latex'}:
                key = f'{self.settings.catalog}::{self.name}' if self.settings.catalog else self.name
                response = await self.http.get(
                    self.settings.root+'/api/servers/'+quote(key, safe='')+'/runtime',
                    headers=self.settings.headers, timeout=15)
                if response.status_code != 200:
                    raise RuntimeError('Hub arXiv runtime configuration unavailable')
                environment = response.json().get('data', {}).get('env', {})
                if not isinstance(environment, dict):
                    raise RuntimeError('Invalid Hub arXiv runtime environment')
                allowed = {'MCP_RELAY_URL', 'MCP_RELAY_TOKEN', 'MCP_RELAY_TIMEOUT_SECONDS'}
                env = {k: v for k, v in environment.items() if k in allowed and isinstance(v, str)}
                if bool(env.get('MCP_RELAY_URL')) != bool(env.get('MCP_RELAY_TOKEN')):
                    raise RuntimeError('Hub arXiv Relay URL and token must be configured together')
                if env.get('MCP_RELAY_URL'):
                    previous = self.backend.params.env or {}
                    adapter = str(Path(__file__).with_name('relay_adapter'))
                    self.backend.params.env = {
                        **previous, **env, 'MCPHUB_RELAY_ADAPTER': 'fetch-v1',
                        'PYTHONPATH': adapter+(os.pathsep+previous['PYTHONPATH'] if previous.get('PYTHONPATH') else ''),
                    }
            await self.backend.__aenter__()
            self.entered = True
            tools = await self.backend.list_tools()
            self.binding, callback_token, url = await self.bridge.bind(self.name, self.backend, tools)
            response = await self.http.post(self.settings.root + '/api/task-connections',
                headers=self.settings.headers, json={'server': self.name, 'catalog': self.settings.catalog,
                'taskId': self.bridge.run_id, 'url': url, 'token': callback_token,
                **({'runId': os.environ['MCPHUB_RUN_ID']} if os.getenv('MCPHUB_RUN_ID') else {})}, timeout=15)
            if response.status_code != 200:
                raise RuntimeError(f'Hub task registration rejected for {self.name} (HTTP {response.status_code})')
            body = response.json()
            data = body.get('data')
            if (body.get('success') is not True or not isinstance(data, dict)
                    or not all(isinstance(data.get(k), str) and data[k] for k in ['id', 'token', 'path'])
                    or data['path'] != f"/gateway/v1/tasks/{data['id']}"):
                raise RuntimeError('Invalid Hub task registration response')
            self.connection = data
            self.renewal = asyncio.create_task(self._renew())
        except BaseException:
            await self.cleanup()
            raise

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(120)
                response = await self.http.put(self.settings.root + '/api/task-connections/' + self.connection['id'], headers=self.settings.headers, timeout=15)
                if response.status_code != 200 or response.json().get('success') is not True:
                    raise RuntimeError('Hub task lease renewal rejected')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            await self.bridge.unbind(self.binding)

    async def cleanup(self):
        self.failed = True
        if self.renewal:
            self.renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.renewal
            self.renewal = None
        if self.binding:
            await self.bridge.unbind(self.binding)
            self.binding = ''
        if self.http:
            if self.connection:
                with contextlib.suppress(Exception):
                    await self.http.delete(self.settings.root + '/api/task-connections/' + self.connection['id'], headers=self.settings.headers, timeout=5)
            self.connection = None
            await self.http.aclose()
            self.http = None
        if self.entered:
            self.entered = False
            await self.backend.__aexit__(None, None, None)

    async def _request(self, operation: str, body: dict) -> Any:
        if self.failed or not self.connection or self.http is None:
            raise RuntimeError('Hub task connection unavailable')
        try:
            response = await self.http.post(self.settings.root + self.connection['path'] + '/' + operation,
                headers={'Authorization': 'Bearer ' + self.connection['token']}, json=body)
        except httpx.TimeoutException as error:
            # HTTPX timeouts can have an empty message. Do not include the URL,
            # lease token or request payload, and never replay a possibly executed tool.
            raise RuntimeError(
                f'Hub task request timed out for {self.name} during {operation} '
                f'({type(error).__name__}; configured timeout {self.timeout:g}s)'
            ) from None
        if response.status_code != 200:
            raise RuntimeError(f'Hub task request failed for {self.name} (HTTP {response.status_code})')
        return response.json()

    async def list_tools(self) -> list[Tool]:
        if self.failed:
            raise RuntimeError('Hub task connection unavailable')
        if self.tools is not None and self.cache_tools_list:
            return self.tools
        prefix = self.name + '_'
        values = await self._request('list-tools', {})
        if not isinstance(values, list) or any(not isinstance(t, dict) or not str(t.get('name', '')).startswith(prefix) for t in values):
            raise RuntimeError('Invalid Hub task tools')
        self.tools = [Tool.model_validate({**t, 'name': t['name'][len(prefix):]}) for t in values]
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict | None) -> CallToolResult:
        tools = await self.list_tools()
        if tool_name not in {t.name for t in tools}:
            raise ValueError('Unknown task tool')
        result = await self._request('call-tool', {'tool_name': f'{self.name}_{tool_name}', 'tool_args': arguments or {}})
        detail = (result.get('_meta') or {}).get('mcphub/taskException')
        if result.get('isError') and isinstance(detail, dict):
            if detail.get('kind') == 'mcp':
                raise McpError(ErrorData.model_validate(detail['error']))
            if detail.get('kind') == 'exception' and isinstance(detail.get('message'), str):
                raise RuntimeError(detail['message'])
        return CallToolResult.model_validate(result)
