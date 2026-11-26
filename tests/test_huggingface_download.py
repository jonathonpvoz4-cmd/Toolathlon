import sys
from pathlib import Path
import requests
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.huggingface_download import HuggingFaceAccountAdapter, HuggingFaceAssetAdapter


@pytest.fixture
def routed(monkeypatch):
    for name, value in {'MCP_TOOL_TRANSPORT': 'hub', 'MCPHUB_URL': 'http://hub.test',
                        'MCPHUB_TOKEN': 'group-key', 'MCPHUB_RUN_ID': 'run-one',
                        'MCPHUB_RUN_TOKEN': 'run-token', 'MCPHUB_CATALOG': 'toolathlon'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr('utils.mcp.huggingface_download.time.sleep', lambda _: None)
    monkeypatch.setattr('utils.mcp.hub_sdk_http.mapping', lambda *a, **kw:
                        ('http://hub.test/api/workspace-runs/run-one/http/huggingface/assets',
                         {'Authorization': 'Bearer group-key', 'X-MCPHub-Run-Token': 'run-token'}))


def response(status, request):
    result = requests.Response()
    result.status_code = status
    result._content = b'bytes'
    result._content_consumed = True
    result.request = request
    return result


@pytest.mark.parametrize('kind', ['api', 'assets'])
@pytest.mark.parametrize('method,stream', [('HEAD', False), ('GET', False), ('GET', True)])
def test_download_uses_larger_finite_budget_and_retries_gateway_failure(routed, monkeypatch, kind, method, stream):
    calls, closed = [], []
    def send(self, request, **kwargs):
        calls.append((request, kwargs))
        r = response(504 if len(calls) == 1 else 200, request)
        r.close = lambda: closed.append(r)
        return r
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    adapter = HuggingFaceAccountAdapter('huggingface', 'api') if kind == 'api' else HuggingFaceAssetAdapter('huggingface')
    original = requests.Request(method, 'https://huggingface.co/repo/resolve/main/file?signature=secret', headers={'Range': 'bytes=12-'}).prepare()
    result = adapter.send(original, timeout=10, stream=stream)
    assert result.status_code == 200 and len(calls) == 2 and len(closed) == 1
    for mapped, kwargs in calls:
        assert mapped.url.startswith('http://hub.test/')
        assert mapped.headers['Authorization'] == 'Bearer group-key'
        assert kwargs['proxies'] == {}
    # Platform transport separates admission from execution; local standalone keeps its old adapter deadline.
    if 'X-MCPHub-Upstream-Timeout-Ms' in calls[0][0].headers:
        assert calls[0][0].headers['X-MCPHub-Upstream-Timeout-Ms'] == '120000'
        assert calls[0][1]['timeout'] == (15, 735)
    else:
        assert calls[0][1]['timeout'] == (10, 120)
    assert original.url.endswith('signature=secret')
    if kind == 'api':
        assert next(v for k, v in calls[0][0].headers.items() if k.lower() == 'range') == 'bytes=12-'
        assert calls[0][0].url.endswith('signature=secret')


@pytest.mark.parametrize('status', [401, 403, 404, 429])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_auth_permission_and_rate_errors_are_not_retried(routed, monkeypatch, status, method):
    calls = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', lambda self, req, **kw:
                        calls.append(req) or response(status, req))
    adapter = HuggingFaceAccountAdapter('huggingface', 'api')
    assert adapter.send(requests.Request(method, 'https://huggingface.co/file').prepare(), stream=True, timeout=10).status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize('method,stream', [('POST', True), ('PUT', True), ('PATCH', True), ('DELETE', True)])
def test_write_requests_are_not_retried_or_extended(routed, monkeypatch, method, stream):
    calls = []
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', lambda self, req, **kw:
                        calls.append((req, kw)) or response(504, req))
    adapter = HuggingFaceAccountAdapter('huggingface', 'api')
    adapter.send(requests.Request(method, 'https://huggingface.co/api/repos/delete', data=b'opaque').prepare(), stream=stream, timeout=10)
    assert len(calls) == 1
    if 'X-MCPHub-Upstream-Timeout-Ms' in calls[0][0].headers:
        assert calls[0][0].headers['X-MCPHub-Upstream-Timeout-Ms'] == '10000'
    else:
        assert calls[0][1]['timeout'] == 10


def test_retry_exhaustion_and_longer_caller_budget(routed, monkeypatch):
    calls = []
    def send(self, request, **kwargs):
        calls.append((request, kwargs))
        return response(504, request)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    adapter = HuggingFaceAccountAdapter('huggingface', 'api')
    result = adapter.send(requests.Request('GET', 'https://huggingface.co/file').prepare(), stream=True, timeout=(5, 240))
    assert result.status_code == 504 and len(calls) == 3
    if 'X-MCPHub-Upstream-Timeout-Ms' in calls[0][0].headers:
        assert calls[0][0].headers['X-MCPHub-Upstream-Timeout-Ms'] == '240000'
    else:
        assert calls[0][1]['timeout'] == (5, 240)


def test_network_timeouts_retry_three_times_but_direct_mode_is_unchanged(routed, monkeypatch):
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        raise requests.ReadTimeout('temporary')
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    adapter = HuggingFaceAccountAdapter('huggingface', 'api')
    req = requests.Request('GET', 'https://huggingface.co/file').prepare()
    with pytest.raises(requests.ReadTimeout): adapter.send(req, stream=True, timeout=10)
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    with pytest.raises(requests.ReadTimeout): adapter.send(req, stream=True, timeout=10)
    assert len(calls) == 1 and calls[0].url == req.url


def test_real_streaming_http_retries_504_then_delivers_unchanged_bytes(routed, monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    calls = []
    payload = b'\x00model-download\xff' * 200
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(dict(self.headers))
            if len(calls) == 1:
                self.send_response(504); self.send_header('Content-Length', '0'); self.end_headers()
                return
            time.sleep(.05)
            self.send_response(200); self.send_header('Content-Length', str(len(payload))); self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    target = f'http://127.0.0.1:{server.server_port}/download'
    monkeypatch.setattr('utils.mcp.workspace_http.endpoint', lambda *args:
                        (SimpleNamespace(key='group-key', token='run-token'), target))
    try:
        with requests.Session() as session:
            session.mount('https://huggingface.co/', HuggingFaceAccountAdapter('huggingface', 'api'))
            with session.get('https://huggingface.co/repo/resolve/main/model.bin', stream=True, timeout=.01) as result:
                assert result.status_code == 200
                assert b''.join(result.iter_content(37)) == payload
                assert result.url == 'https://huggingface.co/repo/resolve/main/model.bin'
        assert len(calls) == 2
        assert calls[0]['Authorization'] == 'Bearer group-key'
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_download_accepts_urllib3_total_timeout_before_connect():
    from urllib3.util import Timeout
    from utils.mcp.huggingface_download import download_timeout
    assert download_timeout(Timeout(total=240)) == (240, 240)
    assert download_timeout(Timeout(connect=5, read=180)) == (5, 180)


@pytest.mark.parametrize('mode', ['file', 'snapshot'])
def test_sdk_download_covers_head_redirect_cdn_retry_and_interrupted_range_resume(routed, monkeypatch, tmp_path, mode):
    import hashlib
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from huggingface_hub import configure_http_backend, hf_hub_download, snapshot_download, constants
    from huggingface_hub.utils import reset_sessions
    payload = bytes(range(128)) * 4
    requests_seen = []
    head_calls = 0
    get_calls = 0
    metadata_calls = 0
    revision = 'a' * 40
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            nonlocal head_calls
            head_calls += 1
            requests_seen.append(('HEAD', self.path, dict(self.headers)))
            if head_calls == 1:
                self.send_response(504); self.send_header('Content-Length', '0'); self.end_headers(); return
            if head_calls == 2:
                self.send_response(307); self.send_header('Location', '/api/resolve-cache/owner/repo/model.bin'); self.send_header('Content-Length', '0'); self.end_headers(); return
            self.send_response(302)
            self.send_header('X-Repo-Commit', revision)
            self.send_header('X-Linked-Etag', hashlib.sha256(payload).hexdigest())
            self.send_header('X-Linked-Size', str(len(payload)))
            self.send_header('Location', 'https://cdn.hf.co/probe/model.bin?signature=kept')
            self.send_header('Content-Length', '0'); self.end_headers()
        def do_GET(self):
            nonlocal get_calls, metadata_calls
            if self.path.startswith('/api/models/'):
                metadata_calls += 1
                requests_seen.append(('GET', self.path, dict(self.headers)))
                if metadata_calls == 1:
                    self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers(); return
                body = json.dumps({'id':'owner/repo', 'sha':revision, 'siblings':[{'rfilename':'model.bin'}]}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
            get_calls += 1
            requests_seen.append(('GET', self.path, dict(self.headers)))
            if get_calls == 1:
                self.send_response(504); self.send_header('Content-Length', '0'); self.end_headers(); return
            start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            self.send_response(206 if start else 200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload) - start))
            if start:self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
            self.end_headers()
            if get_calls == 2:
                self.wfile.write(payload[:64]); self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR); self.close_connection = True
            else:self.wfile.write(payload[start:])
        def log_message(self, *args):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    local = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr('utils.mcp.workspace_http.endpoint', lambda server, binding, path='/':
                        (SimpleNamespace(key='group-key', token='run-token'), local + path))
    def asset_mapping(url, headers, *args, **kwargs):
        parsed = urlsplit(url)
        result = {'Authorization': 'Bearer group-key', 'X-MCPHub-Run-Token': 'run-token'}
        result.update({k:v for k,v in headers.items() if k.lower() == 'range'})
        return local + parsed.path + '?' + parsed.query, result
    monkeypatch.setattr('utils.mcp.hub_sdk_http.mapping', asset_mapping)
    monkeypatch.setattr(constants, 'DOWNLOAD_CHUNK_SIZE', 16)
    monkeypatch.setattr(constants, 'HF_HUB_DISABLE_XET', True, raising=False)
    def backend():
        session = requests.Session()
        session.mount('https://', HuggingFaceAssetAdapter('huggingface'))
        session.mount('https://huggingface.co/', HuggingFaceAccountAdapter('huggingface', 'api'))
        return session
    configure_http_backend(backend)
    try:
        if mode == 'file':
            result = hf_hub_download('owner/repo', 'model.bin', cache_dir=tmp_path/'cache',
                                     local_dir=tmp_path/'result', token='upstream-must-not-leak', etag_timeout=.01)
        else:
            folder = snapshot_download('owner/repo', cache_dir=tmp_path/'cache',
                                       local_dir=tmp_path/'result', token='upstream-must-not-leak', etag_timeout=.01)
            result = Path(folder)/'model.bin'
            assert metadata_calls == 2
        assert Path(result).read_bytes() == payload
        assert hashlib.sha256(Path(result).read_bytes()).digest() == hashlib.sha256(payload).digest()
        assert head_calls == 3 and get_calls == 3
        last = requests_seen[-1]
        assert last[2]['Range'] == 'bytes=64-'
        assert last[1].endswith('signature=kept')
        for method, path, headers in requests_seen:
            assert headers['Authorization'] == 'Bearer group-key'
            if 'X-MCPHub-Upstream-Timeout-Ms' in headers:
                assert int(headers['X-MCPHub-Upstream-Timeout-Ms']) >= 120000
    finally:
        reset_sessions(); configure_http_backend(); server.shutdown(); server.server_close(); thread.join(2)
