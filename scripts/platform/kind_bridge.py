"""Connect a task to its Run's Kind bridge and export a reachable kubeconfig."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import yaml

ORIGINAL = Path('/usr/local/bin/kind-upstream')


def adapt_cluster(name: str, config: Path):
    node = json.loads(subprocess.check_output(['docker', 'inspect', name + '-control-plane']))[0]
    networks = node['NetworkSettings']['Networks']
    network = next((key for key in networks if key == 'kind'), None)
    if not network:
        raise RuntimeError('Kind node has no private Kind network')
    address = str(ipaddress.ip_address(networks[network]['IPAddress']))
    task = os.environ.get('TOOLATHLON_KIND_AGENT_CONTAINER') or socket.gethostname()
    current = json.loads(subprocess.check_output(['docker', 'inspect', task]))[0]
    if network not in current['NetworkSettings']['Networks']:
        subprocess.run(['docker', 'network', 'connect', network, task], check=True)
    document = yaml.safe_load(config.read_text())
    for entry in document['clusters']:
        cluster = entry['cluster']
        old = urlsplit(cluster['server'])
        if old.hostname not in {'localhost', '127.0.0.1', '::1'}:
            continue
        cluster['server'] = urlunsplit(('https', address + ':6443', old.path, '', ''))
        # Verify the original certificate identity while using a reachable IP.
        cluster['tls-server-name'] = cluster.get('tls-server-name') or old.hostname
    config.write_text(yaml.safe_dump(document, sort_keys=False))
    config.chmod(0o600)
    # Preview apps use explicit NodePort mappings. Publish them only on the
    # task's loopback, forwarding directly to the node on the private bridge.
    mappings = node['HostConfig'].get('PortBindings') or {}
    for container_port, bindings in mappings.items():
        if container_port == '6443/tcp' or not container_port.endswith('/tcp'):
            continue
        destination_port = int(container_port.split('/')[0])
        for binding in bindings or []:
            port = int(binding['HostPort'])
            if port != destination_port:
                raise RuntimeError('Kind app bridge requires matching node and local ports')
            bridge = [sys.executable, str(Path(__file__).with_name('task_bridge.py'))]
            if os.environ.get('TOOLATHLON_KIND_AGENT_CONTAINER'):
                # Keep app forwarding alive in the model container after setup exits.
                subprocess.run(['docker', 'exec', '-d', task, *bridge,
                                '--forward', str(port), '--destination', address], check=True)
                subprocess.run(['docker', 'exec', task, *bridge, '--wait', str(port)], check=True)
            else:
                subprocess.Popen([*bridge, '--forward', str(port), '--destination', address],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run([*bridge, '--wait', str(port)], check=True)


def option(args, name, default):
    for index, arg in enumerate(args):
        if arg.startswith(name + '='):
            return arg.split('=', 1)[1]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return default


def install():
    if not ORIGINAL.exists():
        executable = Path(shutil.which('kind'))
        executable.rename(ORIGINAL)
        executable.write_text('#!/bin/sh\nexec /workspace/.venv/bin/python /workspace/scripts/platform/kind_bridge.py "$@"\n')
        executable.chmod(0o755)


def main():
    args = sys.argv[1:]
    if args == ['--install']:
        install()
        return 0
    code = subprocess.call([str(ORIGINAL), *args])
    if code == 0 and args[:2] == ['create', 'cluster']:
        path = option(args, '--kubeconfig', os.environ.get('KUBECONFIG') or str(Path.home()/'.kube/config'))
        adapt_cluster(option(args, '--name', 'kind'), Path(path))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
