"""Route the pinned GitHub MCP through its supported host/proxy configuration.

The old binary drops ports from GITHUB_HOST. An isolated HTTP proxy lets it use
its original REST/GraphQL/raw clients without patching the binary or tool schemas.
"""
from __future__ import annotations

import hmac
import asyncio
import contextlib
import os
import secrets
from urllib.parse import urlsplit

import httpx
from aiohttp import web

HOST = 'github-mcphub.invalid'
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'set-cookie'}


class GithubAPIProxy:
    def __init__(self, settings, timeout):
        if not os.getenv('MCPHUB_RUN_ID') or not os.getenv('MCPHUB_RUN_TOKEN'):
            raise RuntimeError('GitHub MCP requires a live Hub workspace run')
        self.settings = settings
        self.timeout = timeout
        self.token = secrets.token_hex(32)
        self.runner = None
        self.http = None
        self.proxy_url = ''

    async def start(self):
        self.http = httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False)
        app = web.Application(client_max_size=64*1024*1024)
        app.router.add_route('*', '/{path:.*}', self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, '127.0.0.1', 0).start()
            self.proxy_url = f'http://127.0.0.1:{self.runner.addresses[0][1]}'
        except BaseException:
            await self.close()
            raise
        return self

    @property
    def environment(self):
        return {'GITHUB_HOST': 'http://'+HOST, 'GITHUB_PERSONAL_ACCESS_TOKEN': self.token,
                'HTTP_PROXY': self.proxy_url, 'http_proxy': self.proxy_url,
                'HTTPS_PROXY': self.proxy_url, 'https_proxy': self.proxy_url,
                'NO_PROXY': '', 'no_proxy': '', 'ALL_PROXY': '', 'all_proxy': ''}

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.http:
            await self.http.aclose()
            self.http = None

    async def handle(self, request):
        current = asyncio.current_task()
        async def watch():
            while True:
                await asyncio.sleep(.1)
                if request.transport is None or request.transport.is_closing():
                    current.cancel()
                    return
        watcher = asyncio.create_task(watch())
        try:
            return await self.forward(request)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def forward(self, request):
        if not hmac.compare_digest(request.headers.get('Authorization','').encode(), ('Bearer '+self.token).encode()):
            raise web.HTTPForbidden()
        original = urlsplit(request.raw_path)
        if original.netloc and (original.scheme != 'http' or original.netloc != HOST):
            raise web.HTTPForbidden()
        path = original.path
        if path.startswith('/api/v3/'):
            binding, relative = 'api', path[len('/api/v3'):]
        elif path == '/api/graphql':
            binding, relative = 'api', '/graphql'
        elif path.startswith('/api/uploads/'):
            binding, relative = 'uploads', path[len('/api/uploads'):]
        elif path.startswith('/raw/'):
            binding, relative = 'raw', path[len('/raw'):]
        else:
            raise web.HTTPNotFound()
        if original.query:
            relative += '?' + original.query
        headers = {name:value for name,value in request.headers.items()
                   if name.lower() not in HOP | {'authorization','host','content-length','accept-encoding'}
                   and not name.lower().startswith('x-mcphub-')}
        headers.update(self.settings.headers)
        headers['X-MCPHub-Stage'] = 'tool'
        if os.getenv('MCPHUB_TASK_ID'):
            headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
        url = (self.settings.root+'/api/workspace-runs/'+os.environ['MCPHUB_RUN_ID']+
               '/http/github/'+binding+relative)
        try:
            async with self.http.stream(request.method, url, headers=headers,
                                        content=await request.read()) as upstream:
                response_headers = {k:v for k,v in upstream.headers.items()
                                    if k.lower() not in HOP | {'content-length','content-encoding'}}
                response = web.StreamResponse(status=upstream.status_code, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.aiter_bytes():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (httpx.HTTPError, ConnectionError):
            raise web.HTTPBadGateway(text='GitHub Hub transport failed') from None
