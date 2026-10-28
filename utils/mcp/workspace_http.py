"""Explicit preparation/grading HTTP clients. No global network monkey-patching."""
from __future__ import annotations

import asyncio
import os
import time
import json
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import requests as direct_requests

from utils.mcp.workspace_credentials import CredentialError, WorkspaceClient


def hub_enabled():
    return os.getenv('MCP_TOOL_TRANSPORT', 'direct').strip().lower() == 'hub'


def endpoint(server, binding, path='/'):
    client = WorkspaceClient.from_env()
    # An active run is mandatory: never fall back to a repository's old key.
    return client, f'{client.root}{client.path}/http/{quote(server, safe="")}/{quote(binding, safe="")}{path}'


class GithubRequests:
    """Keep requests.Response/status handling while resolving GitHub auth at Hub."""
    def __getattr__(self, name):
        if name in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
            return lambda url, **kwargs: self.request(name.upper(), url, **kwargs)
        return getattr(direct_requests, name)

    def request(self, method, url, **kwargs):
        if not hub_enabled():
            return direct_requests.request(method, url, **kwargs)
        original = urlsplit(url)
        if original.hostname and original.hostname.endswith('.googleapis.com'):
            from utils.mcp.hub_sdk_http import http_request
            return http_request(method, url, **kwargs)
        if original.hostname not in {'api.github.com', 'github.com'}:
            # This facade is used by files that also query unrelated public services.
            return direct_requests.request(method, url, **kwargs)
        if original.scheme != 'https' or original.username or original.password or original.port not in {None, 443}:
            raise CredentialError('Invalid GitHub upstream URL')
        client, target = endpoint('github', 'api' if original.hostname == 'api.github.com' else 'git', original.path or '/')
        if original.query:
            target += '?' + original.query
        headers = {name: value for name, value in (kwargs.pop('headers', None) or {}).items()
                   if name.lower() in {'accept', 'content-type', 'x-github-api-version'}}
        headers.update({'Authorization': 'Bearer ' + client.key, 'X-MCPHub-Run-Token': client.token})
        if os.getenv('MCPHUB_TASK_ID'):
            headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
        if kwargs.get('auth') is not None:
            kwargs.pop('auth')
        if kwargs.get('cookies'):
            raise CredentialError('Cookies are not supported for GitHub Hub requests')
        # Upstream redirects must not carry Hub credentials to another origin.
        kwargs['allow_redirects'] = False
        for attempt in range(3):
            response = direct_requests.request(method, target, headers=headers, **kwargs)
            try:
                message = str(response.json().get('message', '')).lower()
            except (ValueError, AttributeError):
                message = ''
            limited = response.status_code == 429 or (response.status_code == 403 and
                any(text in message for text in ('secondary rate limit', 'api rate limit exceeded')))
            if not limited or attempt == 2:
                return response
            wait = 60 * (2 ** attempt)
            retry_after = response.headers.get('Retry-After', '')
            try:
                wait = max(wait, float(retry_after))
            except ValueError:
                try:
                    wait = max(wait, parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
            if response.headers.get('X-RateLimit-Remaining') == '0':
                try:
                    wait = max(wait, float(response.headers['X-RateLimit-Reset']) - time.time())
                except (ValueError, KeyError):
                    pass
            response.close()
            time.sleep(wait)


github_requests = GithubRequests()


def notion_endpoint(url, headers):
    original = urlsplit(str(url))
    if original.scheme != 'https' or original.hostname != 'api.notion.com' or original.port not in {None, 443} or original.username or original.password:
        raise CredentialError('Invalid Notion upstream URL')
    token = next((value for name, value in headers.items() if name.lower() == 'authorization'), '')
    if not token.startswith('Bearer '):
        raise CredentialError('Notion request requires the selected account credential')
    # These are the run snapshot's values, never the source repository's config.
    from utils.mcp.run_config import run_config_file
    path = run_config_file('hub-account-values.json')
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        raise CredentialError('Notion Hub account snapshot is unavailable') from None
    for field, binding in [('notion_integration_key', 'prepare'), ('notion_integration_key_eval', 'evaluate')]:
        if values.get(field) and token == 'Bearer ' + values[field]:
            client, target = endpoint('notion', binding, original.path or '/')
            return client, target + ('?' + original.query if original.query else '')
    raise CredentialError('Notion credential does not belong to this run account')


class NotionRequests:
    def __getattr__(self, name):
        if name in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
            return lambda url, **kwargs: self.request(name.upper(), url, **kwargs)
        return getattr(direct_requests, name)

    def request(self, method, url, **kwargs):
        if not hub_enabled():
            return direct_requests.request(method, url, **kwargs)
        if urlsplit(str(url)).hostname != 'api.notion.com':
            return github_requests.request(method, url, **kwargs)
        headers = kwargs.pop('headers', None) or {}
        client, target = notion_endpoint(url, headers)
        headers = {name: value for name, value in headers.items()
                   if name.lower() in {'accept', 'content-type', 'notion-version'}}
        headers.update({'Authorization': 'Bearer ' + client.key, 'X-MCPHub-Run-Token': client.token})
        if os.getenv('MCPHUB_TASK_ID'):
            headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
        kwargs['allow_redirects'] = False
        return direct_requests.request(method, target, headers=headers, **kwargs)


notion_requests = NotionRequests()


class AccountHTTPAdapter(direct_requests.adapters.HTTPAdapter):
    """SDK session adapter; redirects retain original URLs, not Hub credentials."""
    def __init__(self, server, binding, **kwargs):
        super().__init__(**kwargs)
        self.server, self.binding = server, binding

    def send(self, request, **kwargs):
        if not hub_enabled():
            return super().send(request, **kwargs)
        original = urlsplit(request.url)
        client, url = endpoint(self.server, self.binding, original.path or '/')
        if original.query:
            url += '?' + original.query
        mapped = request.copy()
        mapped.url = url
        mapped.headers = {name: value for name, value in request.headers.items()
            if name.lower() in {'accept', 'accept-encoding', 'content-encoding', 'content-type', 'content-length', 'range', 'if-none-match', 'if-modified-since', 'user-agent', 'use-admin-privileges'}}
        mapped.headers.update({'Authorization': 'Bearer ' + client.key, 'X-MCPHub-Run-Token': client.token})
        if os.getenv('MCPHUB_TASK_ID'):
            mapped.headers['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
        kwargs['proxies'] = {}
        response = super().send(mapped, **kwargs)
        response.request = request
        response.url = request.url
        return response


class AssetHTTPAdapter(direct_requests.adapters.HTTPAdapter):
    """Signed SDK storage requests use declared public origins and the same run."""
    def __init__(self, server, **kwargs):
        super().__init__(**kwargs)
        self.server = server

    def send(self, request, **kwargs):
        if not hub_enabled(): return super().send(request, **kwargs)
        from utils.mcp.hub_sdk_http import mapping
        selected = mapping(request.url, request.headers, request.body, server=self.server)
        if selected is None:
            raise CredentialError('SDK storage origin has no authorized Hub binding')
        mapped = request.copy()
        mapped.url, mapped.headers = selected
        kwargs['proxies'] = {}
        response = super().send(mapped, **kwargs)
        response.request, response.url = request, request.url
        return response


async def github_git(operation, full_name, local_dir):
    """Git smart HTTP through Hub; capabilities are environment-only, never URL/argv."""
    if operation not in {'clone', 'push'} or len(full_name.split('/')) != 2:
        raise ValueError('Invalid GitHub Git operation')
    if any(part in {'', '.', '..'} for part in full_name.split('/')):
        raise ValueError('Invalid GitHub repository')
    path = '/' + '/'.join(quote(part, safe='') for part in full_name.split('/')) + '.git'
    client, url = endpoint('github', 'git', path)
    env = dict(os.environ)
    index = int(env.get('GIT_CONFIG_COUNT', '0'))
    for key, value in [
        (f'http.{client.root}/.extraHeader', 'Authorization: Bearer ' + client.key),
        (f'http.{client.root}/.extraHeader', 'X-MCPHub-Run-Token: ' + client.token),
        ('http.followRedirects', 'false'),
    ]:
        env[f'GIT_CONFIG_KEY_{index}'] = key
        env[f'GIT_CONFIG_VALUE_{index}'] = value
        index += 1
    env['GIT_CONFIG_COUNT'] = str(index)
    env['GIT_TERMINAL_PROMPT'] = '0'
    command = ['git', 'clone', '--mirror', url, str(local_dir)] if operation == 'clone' else [
        'git', '-C', str(local_dir), 'push', '--mirror', url]
    process = await asyncio.create_subprocess_exec(*command, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    if process.returncode:
        # Git diagnostics may include remotes; keep capabilities out of task logs.
        raise RuntimeError(f'GitHub Git {operation} through Hub failed (exit {process.returncode})')
