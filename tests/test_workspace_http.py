import asyncio
import os
import subprocess
import threading
import types
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.workspace_http import github_requests, github_git, notion_requests
from utils.mcp.workspace_credentials import CredentialError


@pytest.fixture
def hub(monkeypatch):
    for key, value in {'MCP_TOOL_TRANSPORT': 'hub', 'MCPHUB_URL': 'http://192.168.0.22:3003',
        'MCPHUB_TOKEN': 'hub-group-key', 'MCPHUB_TARGET': 'account-a', 'MCPHUB_CATALOG': 'toolathlon',
        'MCPHUB_RUN_ID': 'run-a', 'MCPHUB_RUN_TOKEN': 'run-capability-a'}.items():
        monkeypatch.setenv(key, value)


def test_github_rewrites_requests_without_sending_upstream_key(hub, monkeypatch):
    calls = []
    response = requests.Response()
    response.status_code = 403
    response._content = b'{"message":"secondary rate limit"}'
    response.headers['Retry-After'] = '60'
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response
    monkeypatch.setattr(requests, 'request', request)
    result = github_requests.post('https://api.github.com/user/repos',
        headers={'Authorization': 'token upstream-secret', 'Accept': 'application/json'},
        json={'name': 'task-a'}, timeout=500)
    assert result is response
    assert result.status_code == 403 and result.headers['Retry-After'] == '60'
    assert len(calls) == 1
    assert calls[0][1] == 'http://192.168.0.22:3003/api/workspace-runs/run-a/http/github/api/user/repos'
    assert 'upstream-secret' not in repr(calls)
    assert calls[0][2]['headers']['Authorization'] == 'Bearer hub-group-key'
    assert calls[0][2]['headers']['X-MCPHub-Run-Token'] == 'run-capability-a'
    assert calls[0][2]['timeout'] == 500
    assert calls[0][2]['allow_redirects'] is False


def test_no_run_or_hub_failure_never_falls_back(hub, monkeypatch):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(args)
        raise requests.ConnectionError('unavailable')
    monkeypatch.setattr(requests, 'request', unavailable)
    with pytest.raises(requests.ConnectionError):
        github_requests.get('https://api.github.com/user')
    assert len(calls) == 1 and calls[0][1].startswith('http://192.168.0.22:3003/')
    monkeypatch.delenv('MCPHUB_RUN_TOKEN')
    with pytest.raises(CredentialError):
        github_requests.get('https://api.github.com/user')
    assert len(calls) == 1


def test_permission_403_is_not_retried(hub, monkeypatch):
    response = requests.Response()
    response.status_code = 403
    response._content = b'{"message":"Resource not accessible by personal access token"}'
    calls = []
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: calls.append(a) or response)
    assert github_requests.get('https://api.github.com/user').status_code == 403
    assert len(calls) == 1


@pytest.fixture(params=['regular', 'namespace'])
def notion_account(hub, monkeypatch, tmp_path, request):
    (tmp_path/'hub-account-values.json').write_text(json.dumps({
        'notion_integration_key': 'source-key', 'notion_integration_key_eval': 'evaluation-key',
    }))
    monkeypatch.setitem(sys.modules, 'configs', types.SimpleNamespace(__file__=str(tmp_path/'__init__.py') if request.param == 'regular' else None, __path__=[str(tmp_path)]))


def test_notion_roles_are_distinct_and_foreign_keys_are_rejected(notion_account, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: calls.append((a, kw)))
    for key, role in [('source-key', 'prepare'), ('evaluation-key', 'evaluate')]:
        notion_requests.post('https://api.notion.com/v1/search',
            headers={'Authorization': 'Bearer ' + key, 'Notion-Version': '2022-06-28'}, json={})
        assert calls[-1][0][1].endswith(f'/http/notion/{role}/v1/search')
        assert calls[-1][1]['headers']['Authorization'] == 'Bearer hub-group-key'
        assert key not in repr(calls[-1])
    with pytest.raises(CredentialError):
        notion_requests.get('https://api.notion.com/v1/users/me', headers={'Authorization': 'Bearer account-b-key'})
    assert len(calls) == 2


def test_notion_sdk_retains_response_and_routes_through_hub(notion_account, monkeypatch):
    pytest.importorskip('notion_client')
    import httpx
    from utils.mcp.hub_notion import Client
    calls = []
    def dispatch(request):
        calls.append(request)
        return httpx.Response(200, json={'object': 'user', 'id': 'notion-user'})
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request',
                        lambda *_: pytest.fail('Notion SDK attempted a direct network request'))
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda: httpx.MockTransport(dispatch))
    with Client(auth='evaluation-key') as client:
        assert client.users.me()['id'] == 'notion-user'
    assert str(calls[0].url).endswith('/http/notion/evaluate/v1/users/me')
    assert calls[0].headers['Authorization'] == 'Bearer hub-group-key'
    assert calls[0].headers['X-MCPHub-Run-Token'] == 'run-capability-a'


def test_huggingface_sdk_uses_hub_backend(hub, monkeypatch):
    pytest.importorskip('huggingface_hub')
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert request.url.startswith('http://192.168.0.22:3003/api/workspace-runs/run-a/http/huggingface/api/')
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"name":"test-user","type":"user","orgs":[]}'
        response.request = request
        response.url = request.url
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    from utils.mcp.hub_huggingface import whoami
    assert whoami(token='upstream-hf-token')['name'] == 'test-user'
    assert calls[0].headers['Authorization'] == 'Bearer hub-group-key'
    assert 'upstream-hf-token' not in repr(dict(calls[0].headers))


def test_wandb_verification_and_queries_use_hub(hub, monkeypatch, tmp_path):
    pytest.importorskip('wandb')
    monkeypatch.setenv('WANDB_API_KEY', 'a'*40)
    monkeypatch.setenv('NETRC', str(tmp_path/'netrc'))
    monkeypatch.setenv('WANDB_CONFIG_DIR', str(tmp_path/'wandb-config'))
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert '/http/wandb/api/graphql' in request.url
        assert request.headers['Authorization'] == 'Bearer hub-group-key'
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":{"viewer":{"id":"test-user","email":"test@example.invalid","entity":"test-entity","flags":"{}","teams":{"edges":[]}}}}'
        response.request = request
        response.url = request.url
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    import wandb
    from wandb.sdk.internal.internal_api import Api as InternalApi
    # Restore the SDK's transport aliases after this test.
    for cls in [wandb.Api, InternalApi]:
        monkeypatch.setitem(cls.__init__.__globals__, 'GraphQLSession', cls.__init__.__globals__['GraphQLSession'])
    from utils.mcp.hub_wandb import Api
    api = Api()
    assert api.client.execute(wandb.Api.__init__.__globals__['gql']('query { viewer { id } }'))['viewer']['id'] == 'test-user'
    assert calls


def test_direct_mode_unchanged(monkeypatch):
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    calls = []
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: calls.append((a, kw)))
    github_requests.get('https://api.github.com/user', headers={'Authorization': 'token direct'})
    assert calls == [(('GET', 'https://api.github.com/user'), {'headers': {'Authorization': 'token direct'}})]


@pytest.mark.asyncio
async def test_git_uses_hub_and_keeps_keys_out_of_argv(hub, monkeypatch):
    calls = []
    class Process:
        returncode = 0
        async def communicate(self): return b'', b''
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    await github_git('clone', 'owner/repo', '/tmp/repo')
    argv, kwargs = calls[0]
    assert argv[:3] == ('git', 'clone', '--mirror')
    assert '/http/github/git/owner/repo.git' in argv[3]
    assert 'hub-group-key' not in repr(argv) and 'run-capability-a' not in repr(argv)
    assert kwargs['env']['GIT_CONFIG_VALUE_0'] == 'Authorization: Bearer hub-group-key'
    assert kwargs['env']['GIT_CONFIG_VALUE_1'] == 'X-MCPHub-Run-Token: run-capability-a'


@pytest.mark.asyncio
async def test_real_git_clone_and_push_over_authenticated_http(hub, monkeypatch, tmp_path):
    """Exercise Git's real header handling and binary smart-HTTP request bodies."""
    origin = tmp_path/'origin'
    origin.mkdir()
    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True)
    seed = tmp_path/'seed'
    git('init', '-q', seed)
    (seed/'data.bin').write_bytes(bytes(range(256))*16)
    git('-C', seed, 'add', '.')
    git('-C', seed, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'seed')
    (origin/'owner').mkdir()
    git('clone', '--bare', seed, origin/'owner/source.git')
    git('init', '--bare', '-q', origin/'owner/destination.git')
    git('--git-dir', origin/'owner/destination.git', 'config', 'http.receivepack', 'true')
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
        def dispatch(self):
            if (self.headers.get('Authorization') != 'Bearer hub-group-key' or
                    self.headers.get('X-MCPHub-Run-Token') != 'run-capability-a'):
                self.send_error(403)
                return
            prefix = '/api/workspace-runs/run-a/http/github/git'
            assert self.path.startswith(prefix)
            path, _, query = self.path[len(prefix):].partition('?')
            received.append((self.command, path))
            env = {**os.environ, 'GIT_PROJECT_ROOT': str(origin), 'GIT_HTTP_EXPORT_ALL': '1',
                'REQUEST_METHOD': self.command, 'PATH_INFO': path, 'QUERY_STRING': query,
                'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                'CONTENT_LENGTH': self.headers.get('Content-Length', '0'), 'REMOTE_USER': 'test'}
            data = self.rfile.read(int(env['CONTENT_LENGTH']))
            result = subprocess.run(['git', 'http-backend'], env=env, input=data, capture_output=True, check=True)
            head, body = result.stdout.split(b'\r\n\r\n', 1)
            headers = [line.decode().split(':', 1) for line in head.split(b'\r\n')]
            status = next((int(value.strip().split()[0]) for name, value in headers if name.lower() == 'status'), 200)
            self.send_response(status)
            for name, value in headers:
                if name.lower() != 'status': self.send_header(name, value.strip())
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('MCPHUB_URL', f'http://127.0.0.1:{server.server_port}')
    try:
        mirror = tmp_path/'mirror'
        await github_git('clone', 'owner/source', mirror)
        await github_git('push', 'owner/destination', mirror)
        source_hash = git('--git-dir', origin/'owner/source.git', 'rev-parse', 'HEAD').stdout
        assert git('--git-dir', origin/'owner/destination.git', 'rev-parse', 'HEAD').stdout == source_hash
        config = (mirror/'config').read_text()
        assert 'hub-group-key' not in config and 'run-capability-a' not in config
        assert any(path.endswith('git-upload-pack') for _, path in received)
        assert any(path.endswith('git-receive-pack') for _, path in received)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
