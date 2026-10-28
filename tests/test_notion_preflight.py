import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.app_specific.notion import notion_token_refresh as refresh
from utils.app_specific.notion import notion_remove_page as remove
from utils.app_specific.notion import notion_task_reset as reset


@pytest.fixture
def saved_auth(tmp_path):
    directory = tmp_path/'mcp-remote-0.1.16'
    directory.mkdir()
    digest = refresh.hashlib.md5(refresh.SERVER_URL.encode()).hexdigest()
    path = directory/(digest + '_tokens.json')
    path.write_text(json.dumps({'access_token': 'old-access', 'refresh_token': 'old-refresh', 'token_type': 'Bearer'}))
    path.with_name(digest + '_client_info.json').write_text(json.dumps({'client_id': 'client', 'token_endpoint_auth_method': 'none'}))
    return tmp_path, path


def responder(response):
    def request(url, **kwargs):
        if not kwargs:
            return {'token_endpoint': 'https://mcp.notion.com/token'}
        assert kwargs['fields']['refresh_token'] == 'old-refresh'
        assert kwargs['fields']['client_id'] == 'client'
        return response
    return request


def test_refresh_persists_rotated_tokens_atomically(saved_auth):
    root, path = saved_auth
    assert refresh.refresh_saved_authorization(root, responder({'access_token': 'new', 'refresh_token': 'rotated', 'expires_in': 3600})) == 'refreshed'
    assert json.loads(path.read_text())['refresh_token'] == 'rotated'
    assert path.stat().st_mode & 0o777 == 0o600


def test_refresh_without_rotated_refresh_token_preserves_existing_one(saved_auth):
    root, path = saved_auth
    assert refresh.refresh_saved_authorization(root, responder({'access_token': 'new'})) == 'refreshed'
    assert json.loads(path.read_text())['refresh_token'] == 'old-refresh'


def test_revoked_refresh_reports_rejection_without_deleting_state(saved_auth):
    root, path = saved_auth
    before = path.read_bytes()
    assert refresh.refresh_saved_authorization(root, responder({'error': 'invalid_grant'})) == 'rejected'
    assert path.read_bytes() == before


@pytest.mark.parametrize('response', [{}, {'error': 'temporarily_unavailable'}, {'access_token': 'new', 'refresh_token': ''}])
def test_ambiguous_refresh_failure_retains_authorization(saved_auth, response):
    root, path = saved_auth
    before = path.read_bytes()
    with pytest.raises(refresh.RefreshCheckError):
        refresh.refresh_saved_authorization(root, responder(response))
    assert path.read_bytes() == before


def test_network_failure_is_not_reauthorization(saved_auth):
    root, path = saved_auth
    before = path.read_bytes()
    def request(*args, **kwargs):
        raise refresh.RefreshCheckError('Network unavailable')
    with pytest.raises(refresh.RefreshCheckError):
        refresh.refresh_saved_authorization(root, request)
    assert path.read_bytes() == before


def test_refresh_never_sends_credentials_to_another_host(saved_auth):
    root, path = saved_auth
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        return {'token_endpoint': 'https://untrusted.example/token'}
    with pytest.raises(refresh.RefreshCheckError):
        refresh.refresh_saved_authorization(root, request)
    assert len(calls) == 1


def test_new_account_needs_initial_authorization(tmp_path):
    assert refresh.refresh_saved_authorization(tmp_path) == 'missing'


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_page_listing_failure_cannot_be_mistaken_for_empty_parent(monkeypatch, status):
    monkeypatch.setattr(remove.requests, 'get', Mock(return_value=SimpleNamespace(status_code=status)))
    with pytest.raises(remove.NotionResetError):
        remove.delete_pages_by_title('parent', 'Task', {}, confirm=False)


def test_delete_failure_stops_reset(monkeypatch):
    monkeypatch.setattr(remove, 'get_child_pages', lambda *_: [{'type': 'child_page', 'child_page': {'title': 'Task'}, 'id': 'page'}])
    monkeypatch.setattr(remove.requests, 'delete', Mock(return_value=SimpleNamespace(status_code=403)))
    with pytest.raises(remove.NotionResetError):
        remove.delete_pages_by_title('parent', 'Task', {}, confirm=False)


def test_delete_requires_old_page_to_disappear(monkeypatch):
    monkeypatch.setattr(remove, 'get_child_pages', lambda *_: [{'type': 'child_page', 'child_page': {'title': 'Task'}, 'id': 'page'}])
    monkeypatch.setattr(remove.requests, 'delete', Mock(return_value=SimpleNamespace(status_code=200)))
    with pytest.raises(remove.NotionResetError):
        remove.delete_pages_by_title('parent', 'Task', {}, confirm=False)


SOURCE = 'a' * 32
TARGET = 'b' * 32
NEW = 'c' * 32


def test_reset_refuses_to_delete_template_parent(tmp_path, monkeypatch):
    (tmp_path/'id').write_text('stale-id')
    deletion = Mock()
    monkeypatch.setattr(reset, 'delete_pages_by_title', deletion)
    with pytest.raises(remove.NotionResetError):
        asyncio.run(reset.reset_task_page(SOURCE, SOURCE, 'Task', 'source-key', 'eval-key', tmp_path/'id'))
    deletion.assert_not_called()
    assert not (tmp_path/'id').exists()


@pytest.mark.parametrize('mode', ['delete-fails', 'copy-fails', 'wrong-parent', 'success'])
def test_checked_reset_only_exposes_new_page_after_validation(tmp_path, monkeypatch, mode):
    output = tmp_path/'page-id'
    output.write_text('stale-id')
    monkeypatch.setattr(reset, 'get_child_pages', lambda *_: [{'type': 'child_page', 'child_page': {'title': 'Task'}, 'id': NEW}])
    def delete(*args, **kwargs):
        if mode == 'delete-fails':
            raise remove.NotionResetError('delete refused')
    monkeypatch.setattr(reset, 'delete_pages_by_title', delete)
    calls = []
    async def spawn(*args, **kwargs):
        calls.append(args)
        assert 'source-key' not in args
        assert kwargs['env']['NOTION_TOKEN'] == 'source-key'
        output.write_text(NEW)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b'', b'')), returncode=1 if mode == 'copy-fails' else 0)
    monkeypatch.setattr(reset.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(reset.requests, 'get', lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {'parent': {'page_id': SOURCE if mode == 'wrong-parent' else TARGET}}))
    call = reset.reset_task_page(SOURCE, TARGET, 'Task', 'source-key', 'eval-key', output)
    if mode == 'success':
        asyncio.run(call)
        assert output.read_text() == NEW
    else:
        with pytest.raises(remove.NotionResetError):
            asyncio.run(call)
        assert not output.exists()
    if mode == 'delete-fails':
        assert calls == []


@pytest.mark.parametrize('auth_exit', [0, 1])
def test_round_authorization_precedes_deployment_and_failure_stops_it(tmp_path, monkeypatch, auth_exit):
    path = Path(__file__).parents[1]/'scripts/n_run.py'
    spec = importlib.util.spec_from_file_location('notion_round_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def step(command, log):
        script = next(part for part in command if part.endswith('.sh'))
        calls.append(script)
        return auth_exit if script == module.BOOTSTRAP_SCRIPT else 0
    monkeypatch.setattr(module, 'run_step', step)
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    monkeypatch.setattr(sys, 'argv', ['n_run.py', '--N', '1', '--model_name', 'fixture', '--result_output_path', str(tmp_path), '--api-key', 'fixture'])
    assert module.main() == auth_exit
    assert calls[0] == module.BOOTSTRAP_SCRIPT
    if auth_exit:
        assert calls == [module.BOOTSTRAP_SCRIPT]
    else:
        assert calls == [module.BOOTSTRAP_SCRIPT, module.DEPLOY_SCRIPT, module.RUN_SCRIPT]


@pytest.mark.parametrize('value', ['aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'https://www.notion.so/Page-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?source=copy', 'https://www.notion.so/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'])
def test_page_identity_accepts_existing_notion_url_and_uuid_formats(value):
    assert reset.page_id(value) == SOURCE


@pytest.mark.parametrize('status,error,reauthorize', [(400, 'invalid_grant', True), (401, 'invalid_client', False), (429, 'rate_limited', False), (500, 'server_error', False)])
def test_http_error_classification_is_narrow(monkeypatch, status, error, reauthorize):
    import io
    from urllib.error import HTTPError
    class Opener:
        def open(self, request, timeout):
            assert request.get_header('User-agent') == 'Toolathlon-Notion-Preflight/1.0'
            raise HTTPError(request.full_url, status, 'failure', {}, io.BytesIO(json.dumps({'error': error}).encode()))
    monkeypatch.setattr(refresh, 'build_opener', lambda *_: Opener())
    if reauthorize:
        assert refresh.request_json('https://mcp.notion.com/token', fields={'refresh_token': 'fixture'}) == {'error': 'invalid_grant'}
    else:
        with pytest.raises(refresh.RefreshCheckError):
            refresh.request_json('https://mcp.notion.com/token', fields={'refresh_token': 'fixture'})


@pytest.mark.parametrize('auth_status,sync_status', [(0, 0), (1, 0), (0, 1)])
def test_bootstrap_saves_even_rotated_authorization_before_reporting_probe_failure(tmp_path, auth_status, sync_status):
    import os
    import subprocess
    import shutil
    root = Path(__file__).parents[1]
    (tmp_path/'scripts').mkdir()
    (tmp_path/'configs/.mcp-auth').mkdir(parents=True)
    (tmp_path/'configs/.mcp-auth/notion.env').write_text('NOTION_ACCOUNT_EMAIL=fixture@example.test\n')
    (tmp_path/'utils/app_specific/notion').mkdir(parents=True)
    (tmp_path/'utils/app_specific/notion/notion_oauth_autoauth.py').write_text('')
    shutil.copy2(root/'scripts/bootstrap_notion_official.sh', tmp_path/'scripts/bootstrap_notion_official.sh')
    binary = tmp_path/'bin'
    binary.mkdir()
    record = tmp_path/'commands'
    uv = binary/'uv'
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_RECORD"\ncase "$*" in *workspace_checkpoint*) exit "$SYNC_STATUS";; *) exit "$AUTH_STATUS";; esac\n')
    uv.chmod(0o700)
    result = subprocess.run(['bash', 'scripts/bootstrap_notion_official.sh'], cwd=tmp_path,
        env={**os.environ, 'PATH': str(binary) + ':' + os.environ['PATH'], 'COMMAND_RECORD': str(record),
             'TOOLATHLON_HUB_SNAPSHOT': '1', 'AUTH_STATUS': str(auth_status), 'SYNC_STATUS': str(sync_status)}, capture_output=True)
    assert result.returncode == (sync_status or auth_status)
    commands = record.read_text().splitlines()
    assert '--verify-refresh' in commands[0]
    assert '--force-round-trip' not in commands[0]
    assert 'workspace_checkpoint' in commands[1]


@pytest.mark.parametrize('scenario', ['existing', 'rejected', 'missing', 'network', 'new-refresh-failure'])
def test_real_oauth_entrypoint_preflight_control_flow_without_real_login(tmp_path, scenario):
    import subprocess
    root = Path(__file__).parents[1]
    python = root/'.venv/bin/python'
    if not python.exists():
        pytest.skip('Toolathlon environment with Playwright not installed')
    code = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from utils.app_specific.notion import notion_oauth_autoauth as module
from utils.app_specific.notion import notion_token_refresh as refresh
root, scenario = Path(sys.argv[1]), sys.argv[2]
module.parse_args = lambda: SimpleNamespace(auth_dir=root, env_file=None, mailbox='outlook', lock_held=True, verify_refresh=True, force_round_trip=False, force_notion_login=False, callback_port=1234, timeout_seconds=1)
class Context:
    def __enter__(self): return None
    def __exit__(self, *args): pass
module.sync_playwright = Context
module._launch_chromium = lambda _: SimpleNamespace(close=lambda: None)
module.BrowserAuthorizer = lambda *a, **kw: None
clear = Mock()
module._clear_stale_credentials = clear
probe = Mock(side_effect=['valid'] if scenario == 'existing' else ['reauthorized', 'valid'])
module.ensure_auth = probe
results = {'existing':['refreshed'], 'rejected':['rejected','refreshed'], 'missing':['missing','refreshed'], 'new-refresh-failure':['missing','rejected']}
if scenario == 'network':
    refresh.refresh_saved_authorization = Mock(side_effect=refresh.RefreshCheckError('network unavailable'))
else:
    refresh.refresh_saved_authorization = Mock(side_effect=results[scenario])
result = module.main()
assert result == (1 if scenario in ['network','new-refresh-failure'] else 0)
assert clear.call_count == (1 if scenario == 'rejected' else 0)
if scenario == 'network': assert probe.call_count == 0
elif scenario == 'existing': assert probe.call_count == 1
elif scenario == 'new-refresh-failure': assert probe.call_count == 1
else: assert probe.call_count == 2
'''
    result = subprocess.run([str(python), '-c', code, str(tmp_path), scenario], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('code', [0, 1])
def test_reset_command_stops_at_the_real_failure(code, monkeypatch, capsys):
    import types
    # Avoid loading the benchmark's unrelated model/provider configuration.
    helper=types.SimpleNamespace(run_command=AsyncMock(return_value=(
        'completed' if code == 0 else '',
        'Notion task reset failed (TypeError); agent must not start.\nprovider-secret-diagnostic', code)))
    monkeypatch.setitem(sys.modules,'utils.general.helper',helper)
    if code:
        with pytest.raises(remove.NotionResetError,match='task preparation stopped'):
            asyncio.run(reset.run_reset_command('reset-fixture'))
        error=capsys.readouterr().err
        assert 'TypeError' in error and 'provider-secret-diagnostic' not in error
    else:
        assert asyncio.run(reset.run_reset_command('reset-fixture'))[2] == 0


def test_all_notion_preprocessors_check_reset_exit_before_using_page_ids():
    import ast
    root=Path(__file__).parents[1]/'tasks/finalpool'
    checked=[]
    for path in root.glob('*/preprocess/main.py'):
        text=path.read_text()
        if 'notion_remove_and_duplicate' not in text: continue
        tree=ast.parse(text)
        calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and
               isinstance(node.func,ast.Name) and node.func.id=='run_reset_command']
        assert len(calls)==1,path
        checked.append(path)
    assert len(checked)==8
