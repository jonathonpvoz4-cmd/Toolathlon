import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts.containerized.kubernetes_access import prepare_kubernetes_access, install_agent_identity


def kubeconfig():
    return {'apiVersion': 'v1', 'kind': 'Config',
            'clusters': [{'name': 'task', 'cluster': {'server': 'https://example.invalid',
                                                   'certificate-authority-data': 'CA'}}],
            'contexts': [{'name': 'task', 'context': {'cluster': 'task', 'user': 'admin'}}],
            'current-context': 'task',
            'users': [{'name': 'admin', 'user': {'client-key-data': 'PRIVATE',
                                               'client-certificate-data': 'CERT'}}]}


def bundle(root, task, needed):
    work = root / 'workspace'; work.mkdir()
    return {'resolved_task_config': {'id': 'finalpool-' + task, 'agent_workspace': str(work),
            'needed_mcp_servers': needed, 'local_token_key_session': {'kubeconfig_path': str(root / 'admin.yaml')}}}


def test_non_kubernetes_task_cannot_inherit_cluster_credentials(tmp_path, monkeypatch):
    b = bundle(tmp_path, 'canvas-list-test', ['canvas', 'memory'])
    common = tmp_path / 'deployment/k8s/configs'; common.mkdir(parents=True)
    original = common / 'cluster1-config.yaml'; original.write_text(yaml.safe_dump(kubeconfig()))
    work = Path(b['resolved_task_config']['agent_workspace'])
    copy = work / 'kubeconfig.yaml'; copy.write_text(original.read_text())
    business = work / 'report.yaml'; business.write_text('kind: report\nvalue: 12')
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('KUBECONFIG', str(original))
    monkeypatch.setenv('KUBECONFIG_PATH', str(original))
    with patch('scripts.containerized.kubernetes_access.os.chown'), \
         patch('scripts.containerized.kubernetes_access.Path.home', return_value=tmp_path/'root'), \
         patch('scripts.containerized.kubernetes_access.install_agent_identity') as install:
        prepare_kubernetes_access(b, home, tmp_path)
    import os
    assert 'KUBECONFIG' not in os.environ and 'KUBECONFIG_PATH' not in os.environ
    assert 'kubeconfig_path' not in b['resolved_task_config']['local_token_key_session']
    assert common.stat().st_mode & 0o777 == 0o700
    assert yaml.safe_load(original.read_text()) == kubeconfig()
    assert not copy.exists()
    assert business.read_text() == 'kind: report\nvalue: 12'
    install.assert_not_called()


@pytest.mark.parametrize('state', ['missing', 'null', 'empty'])
def test_tasks_without_local_credentials_keep_native_token_configuration(tmp_path, state):
    b = bundle(tmp_path, 'hk-top-conf', ['fetch', 'filesystem'])
    config = b['resolved_task_config']
    if state == 'missing':
        config.pop('local_token_key_session')
    else:
        config['local_token_key_session'] = None if state == 'null' else {}
    home = tmp_path / 'home'; home.mkdir()
    with patch('scripts.containerized.kubernetes_access.Path.home', return_value=tmp_path/'root'), \
         patch('scripts.containerized.kubernetes_access.install_agent_identity') as install:
        prepare_kubernetes_access(b, home, tmp_path)
    install.assert_not_called()
    if state == 'missing':
        assert 'local_token_key_session' not in config
    elif state == 'null':
        assert config['local_token_key_session'] is None
    else:
        assert config['local_token_key_session'] == {}


def test_task_identity_replaces_workspace_admin_copy_without_touching_grader(tmp_path):
    b = bundle(tmp_path, 'k8s-redis-helm-upgrade', ['k8s', 'terminal'])
    admin = Path(b['resolved_task_config']['local_token_key_session']['kubeconfig_path'])
    admin.write_text(yaml.safe_dump(kubeconfig()))
    work = Path(b['resolved_task_config']['agent_workspace'])
    kept = work / 'k8s_configs/cluster-redis-helm-config.yaml'; kept.parent.mkdir()
    os.link(admin, kept)
    safe = kubeconfig(); safe['users'] = [{'name': 'agent', 'user': {'token': 'RESTRICTED'}}]
    home = tmp_path/'home'; home.mkdir()
    with patch('scripts.containerized.kubernetes_access.os.chown'), \
         patch('scripts.containerized.kubernetes_access.Path.home', return_value=tmp_path/'root'), \
         patch('scripts.containerized.kubernetes_access.install_agent_identity', return_value=safe) as install:
        prepare_kubernetes_access(b, home, tmp_path)
    install.assert_called_once_with(admin, read_only=False, preview=False)
    assert yaml.safe_load(admin.read_text()) == kubeconfig()
    assert yaml.safe_load(kept.read_text()) == safe
    agent_path = Path(b['resolved_task_config']['local_token_key_session']['kubeconfig_path'])
    assert yaml.safe_load(agent_path.read_text()) == safe
    assert agent_path.stat().st_mode & 0o777 == 0o600


def test_audit_only_reads_existing_fixture_and_does_not_gain_exec_or_secret_access():
    applied = []
    def kubectl(config, *args, document=None):
        if args[:2] == ('config', 'view'):return json.dumps(kubeconfig())
        if args[:2] == ('get', 'namespaces'):return json.dumps({'items': [
            {'metadata': {'name': n}} for n in ['default', 'dev', 'production', 'kube-system']]})
        if document:applied.extend(document.get('items', [document]))
        return 'TOKEN'
    with patch('scripts.containerized.kubernetes_access._kubectl', side_effect=kubectl):
        result = install_agent_identity(Path('/admin'), read_only=True)
    assert result['users'] == [{'name': 'toolathlon-agent', 'user': {'token': 'TOKEN'}}]
    assert 'PRIVATE' not in json.dumps(result)
    assert not any(x['kind'] == 'RoleBinding' for x in applied)
    for obj in applied:
        for rule in obj.get('rules', []):
            assert set(rule['verbs']) <= {'get', 'list', 'watch'}
            assert 'secrets' not in rule['resources']
            assert 'pods/exec' not in rule['resources']
            assert 'nodes/proxy' not in rule['resources']


def test_permission_setup_fails_closed_without_logging_credentials():
    import subprocess
    with patch('scripts.containerized.kubernetes_access.subprocess.run',
               return_value=subprocess.CompletedProcess([], 1, '', 'PRIVATE KEY')):
        with pytest.raises(RuntimeError, match='config failed') as error:
            install_agent_identity(Path('/admin'), read_only=False)
    assert 'PRIVATE KEY' not in str(error.value)
