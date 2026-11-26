"""Expose only the task's Kubernetes identity to the unprivileged model phase."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

_READ = ['get', 'list', 'watch']
_WRITE = _READ + ['create', 'update', 'patch', 'delete', 'deletecollection']
_SYSTEM_NAMESPACES = {'kube-system', 'kube-public', 'kube-node-lease', 'local-path-storage'}
_NAME = 'toolathlon-agent'


def _kubectl(config: Path, *args: str, document=None) -> str:
    result = subprocess.run(
        ['kubectl', '--kubeconfig', str(config), '--request-timeout=30s', *args],
        input=json.dumps(document) if document is not None else None,
        capture_output=True, text=True, timeout=45,
    )
    if result.returncode:
        # Kubeconfig and API output can contain credentials; keep them out of logs.
        raise RuntimeError(f'Cannot prepare task Kubernetes permissions: {args[0]} failed')
    return result.stdout


def _object(kind, name, **fields):
    group = 'v1' if kind in {'Namespace', 'ServiceAccount'} else 'rbac.authorization.k8s.io/v1'
    return {'apiVersion': group, 'kind': kind, 'metadata': {'name': name}, **fields}


def install_agent_identity(admin: Path, *, read_only: bool, preview: bool = False) -> dict:
    """Keep admin credentials unchanged; return a separate bounded agent config."""
    original = json.loads(_kubectl(admin, 'config', 'view', '--raw', '--flatten', '--minify', '-o', 'json'))
    if preview:
        _kubectl(admin, 'apply', '-f', '-', document=_object('Namespace', 'pr-preview-123'))
    namespaces = [item['metadata']['name'] for item in json.loads(
        _kubectl(admin, 'get', 'namespaces', '-o', 'json'))['items']]
    business = [name for name in namespaces if name not in _SYSTEM_NAMESPACES]
    # Existing audit fixtures remain unchanged. Admission applies to new Pods;
    # the agent cannot remove these namespace labels or change RBAC.
    for name in business if not read_only else []:
        _kubectl(admin, 'label', 'namespace', name,
                 'pod-security.kubernetes.io/enforce=baseline',
                 'pod-security.kubernetes.io/enforce-version=v1.27', '--overwrite')
    account = _object('ServiceAccount', _NAME)
    account['metadata']['namespace'] = 'default'
    rules = [
        {'apiGroups': [''], 'resources': ['pods', 'pods/log', 'services', 'endpoints',
            'configmaps', 'events', 'persistentvolumeclaims', 'serviceaccounts'], 'verbs': _READ},
        {'apiGroups': ['apps'], 'resources': ['deployments', 'deployments/scale', 'replicasets',
            'statefulsets', 'statefulsets/scale', 'daemonsets'], 'verbs': _READ},
        {'apiGroups': ['batch'], 'resources': ['jobs', 'cronjobs'], 'verbs': _READ},
        {'apiGroups': [''], 'resources': ['nodes', 'namespaces', 'persistentvolumes'], 'verbs': _READ},
        {'apiGroups': ['storage.k8s.io'], 'resources': ['storageclasses'], 'verbs': _READ},
    ]
    subject = {'kind': 'ServiceAccount', 'name': _NAME, 'namespace': 'default'}
    objects = [account, _object('ClusterRole', _NAME + '-read', rules=rules),
        _object('ClusterRoleBinding', _NAME + '-read', subjects=[subject],
                roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': _NAME + '-read'})]
    if not read_only:
        writes = [
            {'apiGroups': [''], 'resources': ['pods', 'pods/log', 'pods/exec', 'pods/attach',
                'pods/portforward', 'services', 'endpoints', 'configmaps', 'secrets',
                'persistentvolumeclaims', 'serviceaccounts', 'replicationcontrollers', 'events'], 'verbs': _WRITE},
            {'apiGroups': ['apps'], 'resources': ['deployments', 'deployments/scale', 'replicasets',
                'statefulsets', 'statefulsets/scale', 'daemonsets'], 'verbs': _WRITE},
            {'apiGroups': ['batch'], 'resources': ['jobs', 'cronjobs'], 'verbs': _WRITE},
            {'apiGroups': ['networking.k8s.io'], 'resources': ['ingresses', 'networkpolicies'], 'verbs': _WRITE},
            {'apiGroups': ['autoscaling'], 'resources': ['horizontalpodautoscalers'], 'verbs': _WRITE},
            {'apiGroups': ['policy'], 'resources': ['poddisruptionbudgets'], 'verbs': _WRITE},
        ]
        objects.append(_object('ClusterRole', _NAME + '-workload', rules=writes))
        for namespace in business:
            binding = _object('RoleBinding', _NAME + '-workload', subjects=[subject],
                roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': _NAME + '-workload'})
            binding['metadata']['namespace'] = namespace
            objects.append(binding)
    _kubectl(admin, 'apply', '-f', '-', document={'apiVersion': 'v1', 'kind': 'List', 'items': objects})
    token = _kubectl(admin, '-n', 'default', 'create', 'token', _NAME, '--duration=24h').strip()
    if not token:
        raise RuntimeError('Task Kubernetes identity has no token')
    result = copy.deepcopy(original)
    result['users'] = [{'name': _NAME, 'user': {'token': token}}]
    for context in result['contexts']:
        context['context']['user'] = _NAME
    return result


def _write_private(path: Path, mode: int):
    if path.is_symlink():
        raise RuntimeError(f'Kubernetes credential path must not be a symlink: {path.name}')
    os.chown(path, 0, 0)
    path.chmod(mode)


def prepare_kubernetes_access(bundle: dict, home: Path, root: Path = Path('/workspace')) -> None:
    config = bundle['resolved_task_config']
    tokens = config.get('local_token_key_session') or {}
    needs_cluster = 'k8s' in config.get('needed_mcp_servers', [])
    workspace = Path(config['agent_workspace'])
    # These files are global setup credentials, never the agent's identity.
    common = root / 'deployment/k8s/configs'
    if common.exists():
        _write_private(common, 0o700)
    original_home = Path.home() / '.kube'
    if original_home.exists():
        _write_private(original_home, 0o700)
    os.environ.pop('KUBECONFIG', None)
    os.environ.pop('KUBECONFIG_PATH', None)
    agent_config = None
    source = tokens.get('kubeconfig_path')
    if needs_cluster:
        if not isinstance(source, str) or not Path(source).is_file():
            raise RuntimeError('Kubernetes task is missing its prepared kubeconfig')
        task_name = str(config.get('task_dir', config.get('id', '')))
        agent_config = install_agent_identity(Path(source), read_only='k8s-safety-audit' in task_name,
                                             preview='k8s-pr-preview-testing' in task_name)
        destination = home / 'kubeconfig.yaml'
        destination.write_text(yaml.safe_dump(agent_config))
        destination.chmod(0o600)
        os.chown(destination, 65532, 65532)
        tokens['kubeconfig_path'] = str(destination)
        os.environ['KUBECONFIG'] = str(destination)
    else:
        tokens.pop('kubeconfig_path', None)
    # Redis Helm retains a workspace copy. Never leave an alternate admin
    # identity beside the restricted one, including copies from a cancelled run.
    for path in workspace.rglob('*'):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        if path.suffix not in {'.yaml', '.yml', '.json'} and 'config' not in path.name.casefold():
            continue
        try:
            value = yaml.safe_load(path.read_text())
        except (ValueError, OSError, yaml.YAMLError):
            continue
        if not isinstance(value, dict) or value.get('kind') != 'Config' or not all(
                key in value for key in ['clusters', 'users', 'contexts']):
            continue
        if agent_config is not None:
            # Replace the inode so a retained hardlink cannot overwrite the
            # trusted config subsequently used by the grader.
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                yaml.safe_dump(agent_config, stream)
                replacement = Path(stream.name)
            replacement.replace(path)
        else:
            path.unlink()
