import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.workspace_credentials import CredentialError, OAuthSync, WorkspaceClient, materialize, safe_file, snapshot


def test_snapshot_keeps_private_task_bindings_without_copying_global_secrets(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source/'.gitignore').write_text('token_key_session.py\nconfigs/*.json\n')
    (source/'configs').mkdir()
    (source/'configs/token_key_session.py').write_text('GLOBAL_SECRET = "do-not-copy"\n')
    (source/'configs/google_credentials.json').write_text('{"secret":"do-not-copy"}')
    task = source/'tasks/finalpool/sample'
    task.mkdir(parents=True)
    original = task/'token_key_session.py'
    original.write_text('resource_path = "task-specific"\n')
    # A symlink must not import unrelated private data into the run.
    linked_task = source/'tasks/finalpool/linked'
    linked_task.mkdir()
    (linked_task/'token_key_session.py').symlink_to(source/'configs/token_key_session.py')
    destination = tmp_path/'run'
    destination.mkdir()
    snapshot(source, destination)
    recovered = destination/'tasks/finalpool/sample/token_key_session.py'
    assert recovered.read_text() == original.read_text()
    assert recovered.stat().st_mode & 0o777 == 0o600
    assert not (destination/'configs/token_key_session.py').exists()
    assert not (destination/'configs/google_credentials.json').exists()
    assert not (destination/'tasks/finalpool/linked/token_key_session.py').exists()
    recovered.write_text('changed by preprocess\n')
    assert original.read_text() == 'resource_path = "task-specific"\n'


def test_credential_files_confined_to_snapshot(tmp_path):
    assert not safe_file('configs/../../outside.json')
    assert not safe_file('configs/token_key_session.py')
    assert not safe_file('/etc/passwd')
    with pytest.raises(CredentialError):
        materialize(tmp_path, {'values': {}, 'files': {'configs/../../outside.json': 'secret'}})
    outside = tmp_path/'outside'
    outside.mkdir()
    (tmp_path/'configs').symlink_to(outside)
    # An external symlink must not receive credential data.
    external = tmp_path.parent/'external-workspace-test'
    external.mkdir(exist_ok=True)
    (tmp_path/'configs').unlink()
    (tmp_path/'configs').symlink_to(external)
    with pytest.raises(CredentialError):
        materialize(tmp_path, {'values': {}, 'files': {'configs/key.json': 'secret'}})
    assert not (external/'key.json').exists()


def test_generated_config_preserves_resource_fields_and_uses_selected_account(tmp_path):
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/__init__.py').write_text('')
    (tmp_path/'configs/token_key_session_example.py').write_text(
        'all_token_key_session = {"github_token":"old", "github_allowed_repos":"null"}\n')
    materialize(tmp_path, {'values': {'github_token': 'workspace-key'},
                          'files': {'configs/google_credentials.json': '{"refresh_token":"one"}'}})
    result = subprocess.check_output([sys.executable, '-c',
        'from configs.token_key_session import all_token_key_session as c; '
        'assert c["github_token"]=="workspace-key"; assert c["github_allowed_repos"]=="null"; '
        'assert c["google_oauth2_token_path"]=="configs/.hub-oauth/google_credentials.json"'], cwd=tmp_path)
    assert result == b''
    assert (tmp_path/'configs/google_credentials.json').is_symlink()
    assert (tmp_path/'configs/hub-account-values.json').stat().st_mode & 0o777 == 0o600


def test_oauth_sync_compares_previous_hash_and_skips_partial_writes(tmp_path):
    old = '{"refresh_token":"old"}'
    materialize(tmp_path, {'values': {}, 'files': {'configs/google_credentials.json': old}})
    class Client:
        path = '/api/workspace-runs/test'
        updates = []
        def request(self, method, path, data):
            self.updates.append(data['updates'])
    client = Client()
    sync = OAuthSync(client, tmp_path, {'configs/google_credentials.json': old})
    (tmp_path/'configs/google_credentials.json').write_text('{')
    sync.sync()
    assert not client.updates
    fresh = '{"refresh_token":"rotated"}'
    (tmp_path/'configs/google_credentials.json').write_text(fresh)
    sync.sync()
    assert client.updates == [[{'path': 'configs/google_credentials.json', 'content': fresh,
                               'expectedHash': hashlib.sha256(old.encode()).hexdigest()}]]
    sync.sync()
    assert len(client.updates) == 1


@pytest.mark.parametrize('checkpoint,sync_fails,interrupt', [(False,False,False),(True,False,False),(True,True,False),(False,False,True)])
def test_original_command_runs_in_private_snapshot_and_returns_oauth(tmp_path, monkeypatch, checkpoint, sync_fails, interrupt):
    source = tmp_path/'source'
    source.mkdir()
    (source/'configs').mkdir()
    (source/'configs/__init__.py').write_text('')
    (source/'configs/token_key_session_example.py').write_text('all_token_key_session = {}\n')
    (source/'task.py').write_text('''from pathlib import Path
from configs.token_key_session import all_token_key_session as c
assert c['github_token'] == 'selected-account'
Path('configs/google_credentials.json').write_text('{"refresh_token":"new"}')
Path('task-result.json').write_text('{"page_id":"generated-by-task"}')
''')
    if interrupt:
        with (source/'task.py').open('a') as stream:
            stream.write('import os, signal, time; os.kill(os.getppid(), signal.SIGINT); time.sleep(60)\n')
    if checkpoint:
        import shutil
        repo = Path(__file__).parents[1]
        (source/'utils/mcp').mkdir(parents=True)
        for name in ['workspace_credentials.py', 'workspace_checkpoint.py']:
            shutil.copy2(repo/'utils/mcp'/name, source/'utils/mcp'/name)
        with (source/'task.py').open('a') as stream:
            stream.write('''
from utils.mcp.workspace_checkpoint import checkpoint
from utils.mcp.workspace_credentials import WorkspaceClient
checkpoint()
assert WorkspaceClient.from_env().credentials()['files']['configs/google_credentials.json'] == '{"refresh_token":"new"}'
''')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    state = {'finished': False, 'files': {'configs/google_credentials.json': '{"refresh_token":"old"}'}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            assert json.loads(self.rfile.read(int(self.headers['Content-Length'])))['workspace'] == 'account-a'
            self.reply({'id': 'run-123', 'token': 'capability'})
        def do_GET(self):
            assert self.headers['X-MCPHub-Run-Token'] == 'capability'
            self.reply({'catalog': 'toolathlon', 'workspace': 'account-a',
                        'values': {'github_token': 'selected-account'}, 'files': state['files']})
        def do_PUT(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if sync_fails and data.get('updates'):
                self.send_response(500); self.end_headers(); return
            for update in data.get('updates', []): state['files'][update['path']] = update['content']
            self.reply(None)
        def do_DELETE(self):
            state['exit_reason'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['exitReason']
            state['finished'] = True
            self.reply(None)
        def reply(self, data):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'data': data}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = Path(__file__).resolve().parents[1]/'scripts/mcphub/run_with_workspace.py'
    spec = importlib.util.spec_from_file_location('workspace_runner', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', source)
    if interrupt:
        class InterruptDuringFinalSync(module.OAuthSync):
            def sync(self, final=False):
                if final:
                    import signal
                    os.kill(os.getpid(), signal.SIGINT)
                return super().sync(final=final)
        monkeypatch.setattr(module, 'OAuthSync', InterruptDuringFinalSync)
    monkeypatch.setenv('MCPHUB_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('MCPHUB_TOKEN', 'group-key')
    monkeypatch.setenv('MCPHUB_TARGET', 'account-a')
    monkeypatch.setenv('MCPHUB_CATALOG', 'toolathlon')
    monkeypatch.setattr(sys, 'argv', [str(path), '--state-dir', str(tmp_path/'runs'), '--', sys.executable, 'task.py'])
    try:
        assert module.main() == (1 if sync_fails else 130 if interrupt else 0)
        assert state['finished'] is not sync_fails
        if not sync_fails: assert state['exit_reason'] == ('cancelled' if interrupt else 'completed')
        assert state['files']['configs/google_credentials.json'] == ('{"refresh_token":"old"}' if sync_fails else '{"refresh_token":"new"}')
        assert not (source/'configs/token_key_session.py').exists()
        run = next((tmp_path/'runs').iterdir())
        assert json.loads((run/'task-result.json').read_text())['page_id'] == 'generated-by-task'
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_snapshot_keeps_ignored_instance_settings_and_hub_can_override(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source/'.gitignore').write_text('configs/ports_config.yaml\nconfigs/port_changes.json\nconfigs/token_key_session.py\n')
    (source/'configs').mkdir()
    (source/'configs/token_key_session_example.py').write_text('all_token_key_session = {}\n')
    (source/'configs/token_key_session.py').write_text('SECRET = "must-not-copy"\n')
    (source/'configs/ports_config.yaml').write_text('instance_suffix: -source\n')
    (source/'configs/port_changes.json').write_text('{"11001": 12001}')
    run = tmp_path/'run';run.mkdir()
    snapshot(source, run)
    assert (run/'configs/ports_config.yaml').read_text() == 'instance_suffix: -source\n'
    assert (run/'configs/port_changes.json').read_text() == '{"11001": 12001}'
    assert not (run/'configs/token_key_session.py').exists()
    materialize(run, {'values': {}, 'files': {'configs/ports_config.yaml': 'instance_suffix: -hub\n'}})
    assert (run/'configs/ports_config.yaml').read_text() == 'instance_suffix: -hub\n'
    assert (source/'configs/ports_config.yaml').read_text() == 'instance_suffix: -source\n'


def test_current_config_template_loads_oauth_and_instance_bindings(tmp_path):
    import shutil
    repo = Path(__file__).resolve().parents[1]
    (tmp_path/'configs').mkdir()
    (tmp_path/'utils/general').mkdir(parents=True)
    shutil.copy2(repo/'configs/token_key_session_example.py', tmp_path/'configs/token_key_session_example.py')
    shutil.copy2(repo/'utils/general/task_resources.py', tmp_path/'utils/general/task_resources.py')
    materialize(tmp_path, {'values': {'github_token': 'hub-account-token'}, 'files': {
        'configs/google_credentials.json': json.dumps({'client_id': 'client-test', 'client_secret': 'secret-test', 'refresh_token': 'refresh-test'}),
        'configs/snowflake_rsa_key.p8': 'test-private-key-fixture',
        'configs/ports_config.yaml': 'instance_suffix: -hub-test\n',
    }})
    code = '''from pathlib import Path
from configs.token_key_session import all_token_key_session as c
assert c.github_token == "hub-account-token"
assert c.google_client_id == "client-test"
assert c.google_client_secret == "secret-test"
assert c.google_refresh_token == "refresh-test"
assert Path(c.snowflake_private_key_path) == Path("configs/snowflake_rsa_key.p8").resolve()
assert c.kubeconfig_path.endswith("deployment/k8s/configs/cluster-hub-test1-config.yaml")
assert c.google_oauth2_token_path == "configs/.hub-oauth/google_credentials.json"
'''
    subprocess.run([sys.executable, '-c', code], cwd=tmp_path, check=True, capture_output=True)


def test_calendar_client_derived_from_selected_oauth_not_another_account(tmp_path):
    import json
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/token_key_session_example.py').write_text('all_token_key_session = {}\n')
    old_client = {'web': {'client_id': 'old-client', 'client_secret': 'old-secret',
                          'project_id': 'preserved-project', 'redirect_uris': ['http://localhost:3000/oauth2callback']}}
    materialize(tmp_path, {'values': {}, 'files': {
        'configs/google_credentials.json': json.dumps({'client_id': 'new-client', 'client_secret': 'new-secret', 'refresh_token': 'new-refresh'}),
        'configs/gcp-oauth.keys.json': json.dumps(old_client),
    }})
    client = json.loads((tmp_path/'configs/gcp-oauth.keys.json').read_text())['web']
    assert client['client_id'] == 'new-client'
    assert client['client_secret'] == 'new-secret'
    assert client['project_id'] == 'preserved-project'
    assert client['token_uri'] == 'https://oauth2.googleapis.com/token'
    assert (tmp_path/'configs/gcp-oauth.keys.json').stat().st_mode & 0o777 == 0o600


def test_all_account_declarations_are_credential_free_and_exported():
    import importlib.util
    import json
    from pathlib import Path
    schema_path = Path(__file__).parents[1]/'scripts/mcphub/credential_schema.py'
    spec = importlib.util.spec_from_file_location('credential_schema', schema_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.SCHEMAS) == 14  # Includes optional notion_official and four managed-only task connections.
    assert sum(bool(s.get('values') or s.get('files')) for s in module.SCHEMAS.values()) == 10
    assert module.SCHEMAS['woocommerce'].keys() == {'managed'}
    assert module.SCHEMAS['huggingface']['values'][0]['name'] == 'huggingface_token'
    google = module.SCHEMAS['google_calendar']['files'][0]
    assert google == module.SCHEMAS['google_forms']['files'][0] == module.SCHEMAS['google_sheet']['files'][0]
    assert json.loads(google['template'])['refresh_token'] == ''
    for schema in module.SCHEMAS.values():
        for field in schema.get('values', []):
            assert not field.get('secret', True) or 'default' not in field
        for file in schema.get('files', []):
            if file.get('format') != 'json':
                continue
            template = json.loads(file.get('template', '{}'))
            for field in file.get('fields', []):
                if field.get('secret', True):
                    assert template.get(field['name'], '') == ''


def test_run_container_cleanup_is_scoped_and_refuses_release_on_failure(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('cleanup_runner', Path(__file__).parents[1]/'scripts/mcphub/run_with_workspace.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/.hub-container-runtime').write_text('docker\n')
    calls = []
    results = ['abc123\n', 'abc123\n', '']
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, results.pop(0), '')
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.stop_task_containers(tmp_path, 'run-a')
    assert calls == [
        ['docker', 'ps', '-q', '--filter', 'label=toolathlon.hub-run=run-a'],
        ['docker', 'stop', '--time', '10', 'abc123'],
        ['docker', 'ps', '-q', '--filter', 'label=toolathlon.hub-run=run-a'],
    ]
    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, '', 'private-runtime-diagnostic')
    monkeypatch.setattr(module.subprocess, 'run', failed)
    with pytest.raises(CredentialError, match='cleanup could not be confirmed') as caught:
        module.stop_task_containers(tmp_path, 'run-a')
    assert caught.value.code == 'cleanup_failed'
    assert 'private-runtime-diagnostic' not in str(caught.value)


def test_workspace_occupied_error_is_specific_and_does_not_print_server_body(monkeypatch):
    import urllib.error
    import io
    def occupied(*args, **kwargs):
        raise urllib.error.HTTPError('http://hub/api/workspace-runs',409,'Conflict',{},io.BytesIO(b'secret-server-diagnostic'))
    monkeypatch.setattr('utils.mcp.workspace_credentials.urlopen', occupied)
    client=WorkspaceClient('http://hub','group-key','toolathlon','account-a')
    with pytest.raises(CredentialError, match='occupied by a previous run') as caught: client.start()
    assert 'secret-server-diagnostic' not in str(caught.value)
