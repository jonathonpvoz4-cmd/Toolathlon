"""Verify refresh capability before task preparation, without logging credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERVER_URL = 'https://mcp.notion.com/mcp'


class RefreshCheckError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, fields=None, headers=None):
    request = Request(url, data=None if fields is None else urlencode(fields).encode(),
                      headers={'User-Agent': 'Toolathlon-Notion-Preflight/1.0', 'Accept': 'application/json', **(headers or {})}, method='GET' if fields is None else 'POST')
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # Only an explicit invalid_grant proves that this grant needs login.
        if fields is not None and exc.code in {400, 401}:
            try:
                body = json.loads(exc.read(65536))
            except (ValueError, OSError):
                body = {}
            if body.get('error') == 'invalid_grant':
                return {'error': 'invalid_grant'}
        raise RefreshCheckError(f'Notion OAuth request failed (HTTP {exc.code}); authorization retained') from None
    except (URLError, OSError, ValueError):
        raise RefreshCheckError('Notion OAuth check unavailable; authorization retained') from None


def atomic_json(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix='.oauth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def refresh_saved_authorization(auth_dir: Path, request=request_json) -> str:
    """Caller must hold the existing Notion cross-process authorization lock.

    Return missing, rejected, or refreshed. Transient/ambiguous failures abort the
    preflight and never delete stored grants or silently fall back to login.
    """
    digest = hashlib.md5(SERVER_URL.encode()).hexdigest()
    candidates = list(auth_dir.glob(f'mcp-remote-*/{digest}_tokens.json'))
    if not candidates:
        return 'missing'
    if len(candidates) != 1:
        raise RefreshCheckError('Multiple Notion authorization versions found; select one before running')
    path = candidates[0]
    try:
        tokens = json.loads(path.read_text())
        client = json.loads(path.with_name(digest + '_client_info.json').read_text())
    except (OSError, ValueError):
        raise RefreshCheckError('Notion authorization files are incomplete; recover authorization first') from None
    if not tokens.get('refresh_token'):
        return 'rejected'
    if not client.get('client_id'):
        raise RefreshCheckError('Notion OAuth client ID is missing')
    metadata = request('https://mcp.notion.com/.well-known/oauth-authorization-server')
    endpoint = metadata.get('token_endpoint')
    url = urlsplit(endpoint or '')
    if (url.scheme != 'https' or url.hostname != 'mcp.notion.com' or
            url.port not in {None, 443} or url.username or url.password or url.fragment):
        raise RefreshCheckError('Unexpected Notion token endpoint; refusing to send credentials')
    fields = {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    method = client.get('token_endpoint_auth_method', 'none')
    if method == 'client_secret_basic':
        if not client.get('client_secret'):
            raise RefreshCheckError('Notion OAuth client secret is missing')
        from urllib.parse import quote
        raw = quote(client['client_id'], safe='') + ':' + quote(client['client_secret'], safe='')
        headers['Authorization'] = 'Basic ' + base64.b64encode(raw.encode()).decode()
    elif method in {'none', 'client_secret_post'}:
        fields['client_id'] = client['client_id']
        if method == 'client_secret_post':
            if not client.get('client_secret'):
                raise RefreshCheckError('Notion OAuth client secret is missing')
            fields['client_secret'] = client['client_secret']
    else:
        raise RefreshCheckError('Unsupported Notion OAuth client authentication method')
    result = request(endpoint, fields=fields, headers=headers)
    if result.get('error') == 'invalid_grant':
        return 'rejected'
    if not isinstance(result.get('access_token'), str) or not result['access_token']:
        raise RefreshCheckError('Notion refresh returned no usable access token')
    if result.get('refresh_token') is not None and (
            not isinstance(result['refresh_token'], str) or not result['refresh_token']):
        raise RefreshCheckError('Notion refresh returned an invalid refresh token')
    # Persist rotating credentials immediately, even if the subsequent MCP probe fails.
    atomic_json(path, {**tokens, **result})
    return 'refreshed'
