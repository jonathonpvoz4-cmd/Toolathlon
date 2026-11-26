"""Optional MCPHub v2 transports; model aliases and task backends stay Toolathlon-owned."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import os
import re
import random
import secrets
import socket
import uuid
from datetime import timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from time import monotonic, time
from asyncio import sleep
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from urllib.parse import quote, urlsplit

import httpx
from utils.mcp.task_transport_error import FAILURE_MARKER, HubTaskTransportError
from utils.mcp.hub_timeouts import (
    hub_http_timeout, HUB_CONTROL_TIMEOUT_SECONDS, HUB_STARTUP_TIMEOUT_SECONDS,
    transport_wait_seconds, discovery_timeout,
)
from aiohttp import web
from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp.types import CallToolResult, Tool

SHARED_SERVERS = frozenset({
    "rail_12306", "google_map", "howtocook", "scholarly",
    "yahoo-finance", "youtube", "youtube-transcript",
})

logger = logging.getLogger(__name__)


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
        # Fetch may read a task-local HTTP server (meeting-assign: localhost:31137).
        # An older explicit shared-server list must not bypass task isolation.
        shared = shared - {"fetch"}
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


def huggingface_hub_params(params: dict[str, Any], settings: HubSettings) -> dict[str, Any]:
    """Use Hub account authorization while retaining the native HF session transport."""
    run = os.getenv('MCPHUB_RUN_ID', '')
    if not run or not os.getenv('MCPHUB_RUN_TOKEN'):
        raise RuntimeError('Hugging Face MCP requires a live Hub workspace run')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run):
        raise RuntimeError('Invalid Hub workspace run ID')
    endpoint = f'{settings.root}/api/workspace-runs/{run}/http/huggingface/api/mcp'
    if str(params.get('url', '')).rstrip('/') not in {'https://huggingface.co/mcp', endpoint}:
        raise RuntimeError('Unsupported Hugging Face MCP endpoint configuration')
    headers = {**settings.headers, 'X-MCPHub-Stage': 'tool'}
    if os.getenv('MCPHUB_TASK_ID'):
        headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
    return {**params, 'url': endpoint, 'headers': headers}


class HubSharedServer(MCPServerStreamableHttp):
    def __init__(self, name: str, settings: HubSettings, config: dict[str, Any]):
        headers = settings.headers
        if settings.target:
            headers["X-MCPHub-Target"] = settings.target
        self.execution_timeout = native_timeout(config)
        self.discovery_timeout = discovery_timeout(config)
        wait = transport_wait_seconds(self.execution_timeout)
        super().__init__(
            name=name,
            params={"url": settings.mcp_url(name), "headers": headers,
                    "sse_read_timeout": timedelta(seconds=wait)},
            cache_tools_list=config.get("cache_tools_list", True),
            client_session_timeout_seconds=wait,
        )
        self._hub_prefix = name + settings.separator

    async def connect(self):
        async with asyncio.timeout(HUB_STARTUP_TIMEOUT_SECONDS):
            await super().connect()

    async def list_tools(self) -> list[Tool]:
        async with asyncio.timeout(transport_wait_seconds(self.discovery_timeout)):
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
                    gateway = os.getenv('TOOLATHLON_CALLBACK_BASE_URL', '').rstrip('/')
                    if gateway:
                        parsed = urlsplit(gateway)
                        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                            raise ValueError('Invalid platform callback URL')
                        self.root = f'{gateway}/callbacks/{socket.gethostname()}/{runner.addresses[0][1]}'
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
        self.lease_ttl = 300.0
        self.lease_deadline = 0.0
        self.failure_reason = 'connection not active'
        self.cache_tools_list = config.get('cache_tools_list', True)
        self.tools: list[Tool] | None = None
        self.github_proxy = None

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
        self.http = httpx.AsyncClient(timeout=hub_http_timeout(self.timeout), follow_redirects=False)
        try:
            if self.name == 'huggingface':
                self.backend.params = huggingface_hub_params(self.backend.params, self.settings)
            if self.name == 'github':
                from utils.mcp.hub_github import GithubAPIProxy
                self.github_proxy = await GithubAPIProxy(self.settings, None).start()
                self.backend.params.env = {**(self.backend.params.env or {}), **self.github_proxy.environment}
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
            async with asyncio.timeout(HUB_STARTUP_TIMEOUT_SECONDS):
                await self.backend.__aenter__()
            self.entered = True
            async with asyncio.timeout(self.timeout):
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
            self.lease_ttl = 300.0
            expires = data.get('expiresAt')
            if isinstance(expires, (int, float)) and not isinstance(expires, bool) and math.isfinite(expires):
                self.lease_ttl = max(0.0, expires / 1000 - time())
            self.lease_deadline = monotonic() + self.lease_ttl
            self.renewal = asyncio.create_task(self._renew())
        except BaseException:
            await self.cleanup()
            raise

    async def _renew(self):
        delay = min(120.0, self.lease_ttl / 3)
        failures = 0
        while True:
            await sleep(min(delay, max(0.0, self.lease_deadline - monotonic())))
            remaining = self.lease_deadline - monotonic()
            if remaining <= 0:
                self._fail_lease('lease expired before renewal could succeed')
                return
            started = monotonic()
            retry_after = 0.0
            try:
                # Only the idempotent lease PUT is retried, never a business call.
                async with asyncio.timeout(min(15.0, remaining)):
                    response = await self.http.put(
                        self.settings.root + '/api/task-connections/' + self.connection['id'],
                        headers=self.settings.headers, timeout=min(15.0, remaining))
                payload = response.json() if response.status_code == 200 else None
                if response.status_code == 200:
                    if not isinstance(payload, dict) or payload.get('success') is not True:
                        raise ValueError('Invalid Hub lease renewal response')
                    self.lease_deadline = started + self.lease_ttl
                    if failures:
                        logger.info('Hub lease renewal recovered: server=%s connection=%s attempts=%d',
                                    self.name, self.connection['id'], failures + 1)
                    failures = 0
                    delay = min(120.0, self.lease_ttl / 3)
                    continue
                reason = f'HTTP {response.status_code}'
                if response.status_code not in {408, 429} and response.status_code < 500:
                    self._fail_lease(reason)
                    return
                try:
                    value = float(response.headers.get('Retry-After', '0'))
                    if math.isfinite(value):
                        retry_after = max(0.0, value)
                except ValueError:
                    pass
            except (httpx.HTTPError, TimeoutError, ValueError) as error:
                reason = type(error).__name__
            failures += 1
            logger.warning('Hub lease renewal failed: server=%s connection=%s reason=%s attempt=%d elapsed=%.1fs lease_remaining=%.1fs',
                           self.name, self.connection['id'], reason, failures, monotonic() - started,
                           max(0.0, self.lease_deadline - monotonic()))
            delay = max(retry_after, min(30.0, 2.0 ** min(failures, 5)) * random.uniform(.8, 1.2))

    def _fail_lease(self, reason):
        self.failed = True
        self.failure_reason = reason
        logger.error('Hub task lease unavailable: server=%s connection=%s reason=%s',
                     self.name, self.connection['id'], reason)

    def _check_connection(self):
        if self.failed or not self.connection or self.http is None:
            raise HubTaskTransportError(f'{FAILURE_MARKER}: server={self.name}; {self.failure_reason}')

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
        try:
            if self.entered:
                self.entered = False
                await self.backend.__aexit__(None, None, None)
        finally:
            if self.github_proxy:
                await self.github_proxy.close()
                self.github_proxy = None

    async def _request(self, operation: str, body: dict) -> Any:
        self._check_connection()
        try:
            response = await self.http.post(self.settings.root + self.connection['path'] + '/' + operation,
                headers={'Authorization': 'Bearer ' + self.connection['token']}, json=body,
                timeout=hub_http_timeout(self.timeout) if operation == 'call-tool' else transport_wait_seconds(self.timeout))
        except httpx.TimeoutException as error:
            # HTTPX timeouts can have an empty message. Do not include the URL,
            # lease token or request payload, and never replay a possibly executed tool.
            raise RuntimeError(
                f'Hub task request timed out for {self.name} during {operation} '
                f'({type(error).__name__}; connection/control limit {HUB_CONTROL_TIMEOUT_SECONDS:g}s; '
                'tool execution uses the configured local MCP deadline)'
            ) from None
        if response.status_code != 200:
            if response.status_code == 403:
                with contextlib.suppress(ValueError):
                    detail = response.json()
                    if isinstance(detail, dict) and detail.get('message') == 'Task connection unavailable':
                        self._fail_lease('Hub rejected the task lease (HTTP 403)')
                        self._check_connection()
            raise RuntimeError(f'Hub task request failed for {self.name} (HTTP {response.status_code})')
        self._check_connection()
        return response.json()

    async def list_tools(self) -> list[Tool]:
        self._check_connection()
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
