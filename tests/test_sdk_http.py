import json
import struct
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(params=['regular', 'namespace'])
def sdk_account(monkeypatch, tmp_path, request):
    for name, value in {'MCP_TOOL_TRANSPORT': 'hub', 'MCPHUB_URL': 'http://192.168.0.22:3003',
        'MCPHUB_TOKEN': 'hub-key', 'MCPHUB_RUN_ID': 'run-sdk', 'MCPHUB_RUN_TOKEN': 'sdk-capability',
        'MCPHUB_CATALOG': 'toolathlon', 'MCPHUB_TARGET': 'account-a'}.items(): monkeypatch.setenv(name, value)
    routes = [
        {'server': 'google-cloud', 'id': identifier, 'url': url, 'field': 'private_key', 'protocol': protocol}
        for identifier, url, protocol in [('storage', 'https://storage.googleapis.com/', 'http'),
            ('bigquery', 'https://bigquery.googleapis.com/', 'http'),
            ('logging', 'https://logging.googleapis.com/', 'http'),
            ('grpc', 'https://logging.googleapis.com/', 'grpc'),
            ('token', 'https://oauth2.googleapis.com/token', 'http')]]
    routes += [{'server': 'google_sheet', 'id': 'oauth', 'url': 'https://oauth2.googleapis.com/token', 'field': 'refresh_token'},
               {'server': 'google_sheet', 'id': 'sheets', 'url': 'https://sheets.googleapis.com/'},
               {'server': 'snowflake', 'id': 'sdk', 'url': 'https://org-account.snowflakecomputing.com/'}]
    (tmp_path/'hub-http-routes.json').write_text(json.dumps(routes))
    monkeypatch.setitem(sys.modules, 'configs', types.SimpleNamespace(__file__=str(tmp_path/'__init__.py') if request.param == 'regular' else None, __path__=[str(tmp_path)]))


def test_google_http_and_refresh_requests_pass_through_hub(sdk_account, monkeypatch):
    pytest.importorskip('google.cloud.storage')
    import requests
    from google.oauth2.credentials import Credentials
    from utils.mcp.hub_google import storage, bigquery, logging
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert request.url.startswith('http://192.168.0.22:3003/api/workspace-runs/run-sdk/http/')
        assert request.headers['Authorization'] == 'Bearer hub-key'
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response._content = b'{"items":[],"datasets":[],"entries":[],"access_token":"refreshed-token","expires_in":3600,"token_type":"Bearer"}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    credentials = Credentials(token='temporary-google-token')
    assert list(storage.Client(project='project-a', credentials=credentials).list_buckets()) == []
    assert list(bigquery.Client(project='project-a', credentials=credentials).list_datasets()) == []
    assert list(logging.Client(project='project-a', credentials=credentials, _use_grpc=False).list_entries()) == []
    assert all(request.headers.get('X-MCPHub-Forward-Authorization') == 'Bearer temporary-google-token' for request in calls)
    from google.auth.transport.requests import Request
    result = Request()(url='https://oauth2.googleapis.com/token', method='POST', body=b'grant_type=refresh_token&refresh_token=temporary-refresh')
    assert result.status == 200
    assert calls[-1].url.endswith('/http/google_sheet/oauth')


def test_google_grpc_uses_original_generated_protobufs_and_errors(sdk_account, monkeypatch):
    pytest.importorskip('grpc')
    import requests
    from google.oauth2.credentials import Credentials
    from google.cloud.logging_v2.types import ListBucketsResponse, LogBucket
    from google.api_core.exceptions import NotFound
    from utils.mcp.hub_google import ConfigServiceV2Client
    calls = []
    status = {'code': '0'}
    def send(self, request, **kwargs):
        calls.append(request)
        assert '/http/google-cloud/grpc/google.logging.v2.ConfigServiceV2/' in request.url
        assert request.headers['Authorization'] == 'Bearer hub-key'
        response = requests.Response()
        response.status_code = 200
        response.headers.update({'grpc-status': status['code'], 'grpc-message': 'not%20found'})
        response.request, response.url = request, request.url
        data = ListBucketsResponse.serialize(ListBucketsResponse(buckets=[LogBucket(name='projects/p/locations/global/buckets/task-a')]))
        response._content = b'\0'+struct.pack('>I', len(data))+data
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    client = ConfigServiceV2Client(credentials=Credentials(token='google-token'))
    buckets = list(client.list_buckets(parent='projects/p/locations/global'))
    assert buckets[0].name.endswith('/task-a')
    status['code'] = '5'
    with pytest.raises(NotFound, match='not found'):
        client.get_bucket(request={'name': 'projects/p/locations/global/buckets/missing'})
    client.transport.close()


def test_google_logging_keeps_native_grpc_and_routes_it(sdk_account, monkeypatch):
    import requests
    from google.oauth2.credentials import Credentials
    from google.cloud.logging_v2.types import ListLogEntriesResponse
    from utils.mcp.hub_google import logging
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert '/http/google-cloud/grpc/google.logging.v2.LoggingServiceV2/ListLogEntries' in request.url
        response = requests.Response()
        response.status_code = 200
        response.headers['grpc-status'] = '0'
        response.request, response.url = request, request.url
        data = ListLogEntriesResponse.serialize(ListLogEntriesResponse())
        response._content = b'\0'+struct.pack('>I', len(data))+data
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    client = logging.Client(project='p', credentials=Credentials(token='google-token'))
    assert list(client.list_entries()) == []
    assert calls
    client.close()


def test_snowflake_native_session_requests_use_hub(sdk_account, monkeypatch):
    pytest.importorskip('snowflake.connector')
    from snowflake.connector.vendored import requests
    from utils.mcp.hub_sdk_http import install_snowflake_http
    install_snowflake_http()
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert '/http/snowflake/sdk/session/v1/login-request' in request.url
        assert request.headers['Authorization'] == 'Bearer hub-key'
        assert request.headers['X-MCPHub-Forward-Authorization'] == 'Snowflake Token="temporary-session"'
        response = requests.Response()
        response.status_code = 200
        response.request, response.url = request, request.url
        response._content = b'{"success":true}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    response = requests.Session().post('https://org-account.snowflakecomputing.com/session/v1/login-request',
        headers={'Authorization': 'Snowflake Token="temporary-session"'}, json={'data': {'TOKEN': 'temporary-jwt'}})
    assert response.json() == {'success': True}
    assert calls


def test_signed_assets_use_declared_account_route(sdk_account, monkeypatch):
    import configs
    from utils.mcp.hub_sdk_http import mapping
    from utils.mcp.run_config import run_config_file
    path = run_config_file('hub-http-routes.json')
    data = json.loads(path.read_text())
    data.append({'server': 'snowflake', 'id': 'assets', 'url': 'https://org-account.snowflakecomputing.com/',
        'assetHosts': ['*.amazonaws.com']})
    path.write_text(json.dumps(data))
    source = 'https://bucket.s3.amazonaws.com/key%2Fvalue?X-Amz-Signature=temporary'
    url, headers = mapping(source, {}, server='snowflake')
    assert url.endswith('/http/snowflake/assets/')
    assert headers['X-MCPHub-Asset-URL'] == source
    assert headers['Authorization'] == 'Bearer hub-key'
    assert 'X-MCPHub-Forward-Authorization' not in headers
    from utils.mcp.workspace_credentials import CredentialError
    with pytest.raises(CredentialError):
        mapping('https://bucket.s3.amazonaws.com.evil.test/x', {'Authorization': 'temporary'}, server='snowflake')


def test_sdk_redirects_never_send_hub_credentials_to_provider(sdk_account, monkeypatch):
    import requests
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials
    from utils.mcp.hub_sdk_http import install_google_http
    install_google_http()
    calls = []
    def send(self, request, **kwargs):
        calls.append(request)
        assert request.url.startswith('http://192.168.0.22:3003/')
        assert request.headers['Authorization'] == 'Bearer hub-key'
        response = requests.Response()
        response.request, response.url = request, request.url
        response.status_code = 302 if len(calls) == 1 else 200
        if len(calls) == 1: response.headers['Location'] = 'https://storage.googleapis.com/redirected'
        response._content = b'{}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    response = AuthorizedSession(Credentials(token='provider-token')).get('https://storage.googleapis.com/original')
    assert response.status_code == 200 and len(calls) == 2
    assert response.url == 'https://storage.googleapis.com/redirected'
    assert calls[-1].headers['X-MCPHub-Forward-Authorization'] == 'Bearer provider-token'


def test_snowflake_origin_is_case_insensitive_but_path_and_other_origins_are_not(sdk_account):
    from utils.mcp.hub_sdk_http import mapping
    from utils.mcp.workspace_credentials import CredentialError
    headers={'Authorization':'Snowflake Token="temporary"'}
    target,_=mapping('https://ORG-ACCOUNT.snowflakecomputing.com:443/session/v1/login-request',headers)
    assert target.endswith('/http/snowflake/sdk/session/v1/login-request')
    target,_=mapping('https://ORG-ACCOUNT.snowflakecomputing.com/CaseSensitivePath',headers)
    assert target.endswith('/CaseSensitivePath')
    for url in ['https://org-account.snowflakecomputing.com.evil.test/session/v1/login-request',
                'https://org-account.snowflakecomputing.com:444/session/v1/login-request',
                'https://user@org-account.snowflakecomputing.com/session/v1/login-request']:
        with pytest.raises(CredentialError): mapping(url,headers)
