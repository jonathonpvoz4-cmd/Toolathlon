"""Opt-in network transports for the pinned Google and Snowflake SDKs.

SDKs keep signing/refreshing their temporary run credentials. Hub owns the live
account capability, fixed origins, request admission, shared scheduling and audit.
"""
from __future__ import annotations
import json
import os
import copy
from urllib.parse import urlsplit, parse_qs
from fnmatch import fnmatchcase

from utils.mcp.workspace_http import hub_enabled, endpoint
from utils.mcp.workspace_credentials import CredentialError
from utils.mcp.run_config import run_config_file

_installed_google = False
_installed_snowflake = False


def routes():
    try:
        value = json.loads(run_config_file('hub-http-routes.json').read_text())
        if not isinstance(value, list): raise ValueError()
        return value
    except (OSError, ValueError):
        raise CredentialError('Hub SDK routing declaration is unavailable; deploy/update registrations first') from None


def same_origin(left, right):
    # DNS names are case-insensitive and HTTPS :443 is the default. Paths,
    # object names and query strings deliberately retain their original bytes.
    try:
        return (not left.username and not left.password and not right.username and not right.password and
                left.scheme == right.scheme and left.hostname == right.hostname and
                (left.port if left.port is not None else (443 if left.scheme == 'https' else 80)) ==
                (right.port if right.port is not None else (443 if right.scheme == 'https' else 80)))
    except ValueError:
        return False


def mapping(url, headers, body=None, *, protocol='http', server=None):
    original = urlsplit(str(url))
    candidates = []
    for route in routes():
        prefix = urlsplit(route['url'])
        if (not route.get('assetHosts') and route.get('protocol', 'http') == protocol and same_origin(original, prefix) and (original.path == prefix.path.rstrip('/') or
                original.path.startswith(prefix.path.rstrip('/')+'/'))):
            candidates.append(route)
    asset = False
    if not candidates and protocol == 'http' and original.scheme == 'https':
        candidates = [route for route in routes() if route.get('assetHosts') and
            (not server or route['server'] == server) and
            any(fnmatchcase((original.hostname or '').lower(), pattern.lower()) for pattern in route['assetHosts'])]
        if len({route['server'] for route in candidates}) > 1:
            raise CredentialError('SDK asset origin needs its account service context')
        asset = bool(candidates)
    if not candidates:
        # Never silently leak an authenticated SDK request around the Hub.
        auth = next((v for k, v in headers.items() if k.lower() == 'authorization'), '')
        if protocol == 'grpc' or auth or original.path.rstrip('/') == '/token':
            raise CredentialError('Authenticated SDK origin has no authorized Hub binding')
        return None
    candidates.sort(key=lambda item: len(urlsplit(item['url']).path), reverse=True)
    if original.path.rstrip('/') == '/token' and isinstance(body, (bytes, str)):
        fields = parse_qs(body.decode() if isinstance(body, bytes) else body)
        kind = 'refresh_token' if 'refresh_token' in fields else 'private_key'
        candidates = [item for item in candidates if item.get('field') == kind]
        if not candidates: raise CredentialError('Hub token request has no matching account credential')
    route = candidates[0]
    prefix = urlsplit(route['url']).path.rstrip('/')
    path = '/' if asset else original.path[len(prefix):]
    if original.query and not asset: path += '?' + original.query
    client, target = endpoint(route['server'], route['id'], path)
    forwarded = {name: value for name, value in headers.items() if name.lower() not in {
        'authorization', 'host', 'content-length', 'cookie', 'connection', 'transfer-encoding'}}
    auth = next((value for name, value in headers.items() if name.lower() == 'authorization'), '')
    forwarded.update({'Authorization': 'Bearer '+client.key, 'X-MCPHub-Run-Token': client.token})
    if auth: forwarded['X-MCPHub-Forward-Authorization'] = auth
    if asset: forwarded['X-MCPHub-Asset-URL'] = str(url)
    if os.getenv('MCPHUB_TASK_ID'): forwarded['X-MCPHub-Task-Id'] = os.environ['MCPHUB_TASK_ID']
    return target, forwarded


def _send_adapter(original_send):
    def send(self, request, **kwargs):
        if not hub_enabled(): return original_send(self, request, **kwargs)
        selected = mapping(request.url, request.headers, request.body)
        if selected is None: return original_send(self, request, **kwargs)
        url, headers = selected
        mapped = request.copy()
        mapped.url, mapped.headers = url, headers
        follow = kwargs.pop('allow_redirects', True)
        response = original_send(self, mapped, allow_redirects=False, **{**kwargs, 'proxies': {}})
        response.request, response.url = request, request.url
        response._next = None
        if follow:
            history = list(self.resolve_redirects(response, request, **kwargs))
            if history:
                history.insert(0, response)
                response = history.pop()
                response.history = history
        else:
            response._next = next(self.resolve_redirects(response, request, yield_requests=True, **kwargs), None)
        return response
    return send


def http_request(method, url, **kwargs):
    import requests
    if not hub_enabled(): return requests.request(method, url, **kwargs)
    headers = kwargs.pop('headers', None) or {}
    selected = mapping(url, headers, kwargs.get('data'))
    if selected: url, headers = selected
    kwargs['allow_redirects'] = False
    return requests.request(method, url, headers=headers, **kwargs)


def install_google_http():
    global _installed_google
    if _installed_google or not hub_enabled(): return
    import google.auth.transport.requests as transport
    import requests
    # Patch the SDK classes, not global requests sessions or unrelated services.
    transport.AuthorizedSession.send = _send_adapter(transport.AuthorizedSession.send)
    original_request = transport.Request.__call__
    def request(self, url, method='GET', body=None, headers=None, timeout=120, **kwargs):
        if not hub_enabled(): return original_request(self, url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        selected = mapping(url, headers or {}, body)
        if selected:
            target, mapped = selected
            response = self.session.request(method, target, data=body, headers=mapped, timeout=timeout, allow_redirects=False, **kwargs)
            return transport._Response(response)
        return original_request(self, url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
    transport.Request.__call__ = request
    import httplib2
    original_http = httplib2.Http.request
    def http_request(self, uri, method='GET', body=None, headers=None, **kwargs):
        if not hub_enabled(): return original_http(self, uri, method=method, body=body, headers=headers, **kwargs)
        selected = mapping(uri, headers or {}, body)
        if selected:
            target, forwarded = selected
            proxy = copy.copy(self)
            proxy.follow_redirects = False
            response, content = original_http(proxy, target, method=method, body=body, headers=forwarded, **kwargs)
            if response.status in {301, 302, 303, 307, 308} and response.get('location') and self.follow_redirects:
                from urllib.parse import urljoin
                from httplib2 import RedirectLimit
                remaining = kwargs.get('redirections', 5)
                if remaining <= 0: raise RedirectLimit('SDK redirect limit reached', response, content)
                destination = urljoin(uri, response['location'])
                old, new = urlsplit(uri), urlsplit(destination)
                if new.scheme != 'https' or old.path.rstrip('/') == '/token':
                    raise CredentialError('Unexpected SDK authorization redirect')
                next_headers = dict(headers or {})
                if old.netloc != new.netloc:
                    next_headers = {k:v for k,v in next_headers.items() if k.lower() not in {'authorization','cookie'}}
                if response.status == 303 or (response.status == 302 and method == 'POST'):
                    method, body = 'GET', None
                return self.request(destination, method=method, body=body, headers=next_headers, **{**kwargs, 'redirections': remaining-1})
            return response, content
        return original_http(self, uri, method=method, body=body, headers=headers, **kwargs)
    httplib2.Http.request = http_request
    _installed_google = True


def install_snowflake_http():
    global _installed_snowflake
    if _installed_snowflake or not hub_enabled(): return
    from snowflake.connector.vendored.requests.sessions import Session
    Session.send = _send_adapter(Session.send)
    _installed_snowflake = True
