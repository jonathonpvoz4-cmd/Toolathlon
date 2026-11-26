import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def bridge_module():
    spec = importlib.util.spec_from_file_location('platform_bridge', Path(__file__).resolve().parents[1] / 'scripts/platform/task_bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_ports_include_kind_api_but_exclude_task_owned_web_port(tmp_path):
    module = bridge_module()
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'global_preparation/deploy_containers.sh'
    path.parent.mkdir()
    path.write_text((root / 'global_preparation/deploy_containers.sh').read_text())
    configs = tmp_path / 'deployment/k8s/configs'
    configs.mkdir(parents=True)
    (configs / 'cluster.yaml').write_text('server: https://127.0.0.1:46321')
    ports = module.service_ports(tmp_path)
    assert 46321 in ports
    assert 11001 in ports
    assert 31137 not in ports
    assert 31123 not in ports
    assert 31124 not in ports


@pytest.mark.parametrize('separate_setup', [False, True])
def test_kind_bridge_uses_private_node_address_and_keeps_tls_validation(tmp_path, monkeypatch, separate_setup):
    import yaml
    spec = importlib.util.spec_from_file_location('kind_bridge', Path(__file__).resolve().parents[1] / 'scripts/platform/kind_bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.socket, 'gethostname', lambda: 'task-container')
    monkeypatch.delenv('TOOLATHLON_KIND_AGENT_CONTAINER', raising=False)
    if separate_setup:
        monkeypatch.setenv('TOOLATHLON_KIND_AGENT_CONTAINER', 'model-container')
    config = tmp_path/'config.yaml'
    config.write_text(yaml.safe_dump({'clusters': [{'name': 'kind-test', 'cluster': {
        'server': 'https://127.0.0.1:38123', 'certificate-authority-data': 'original-ca'}}]}))
    def inspect(args):
        if args[-1] == 'test-control-plane':
            return json.dumps([{'NetworkSettings': {'Networks': {'kind': {'IPAddress': '172.25.0.2'}}},
                                'HostConfig': {'PortBindings': {
                                    '6443/tcp': [{'HostPort': '38123'}],
                                    '31123/tcp': [{'HostPort': '31123'}],
                                }}}]).encode()
        return json.dumps([{'NetworkSettings': {'Networks': {'toolathlon-tasks': {}}}}]).encode()
    connected = []
    spawned = []
    monkeypatch.setattr(module.subprocess, 'check_output', inspect)
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kw: connected.append(args))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda args, **kw: spawned.append((args, kw)))
    module.adapt_cluster('test', config)
    cluster = yaml.safe_load(config.read_text())['clusters'][0]['cluster']
    assert cluster['server'] == 'https://172.25.0.2:6443'
    assert cluster['tls-server-name'] == '127.0.0.1'
    assert cluster['certificate-authority-data'] == 'original-ca'
    assert 'insecure-skip-tls-verify' not in cluster
    target = 'model-container' if separate_setup else 'task-container'
    assert connected[0] == ['docker', 'network', 'connect', 'kind', target]
    if separate_setup:
        assert connected[1][:4] == ['docker', 'exec', '-d', target]
        assert connected[1][-4:] == ['--forward', '31123', '--destination', '172.25.0.2']
        assert connected[2][:3] == ['docker', 'exec', target]
        assert connected[2][-2:] == ['--wait', '31123']
        assert not spawned
        return
    assert connected[1][-2:] == ['--wait', '31123']
    assert spawned[0][0][-4:] == ['--forward', '31123', '--destination', '172.25.0.2']
    # A long-lived forwarder must not keep preprocess.communicate() pipes open.
    assert spawned[0][1] == {
        'start_new_session': True, 'stdin': module.subprocess.DEVNULL,
        'stdout': module.subprocess.DEVNULL, 'stderr': module.subprocess.DEVNULL,
    }


def test_task_loopback_proxy_preserves_bytes():
    module = bridge_module()

    async def run():
        async def echo(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()

        service = await asyncio.start_server(echo, '127.0.0.1', 0)
        port = service.sockets[0].getsockname()[1]
        proxy = asyncio.create_task(module.forward([port], bind='127.0.0.2', destination='127.0.0.1'))
        try:
            for _ in range(50):
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.2', port)
                    break
                except ConnectionRefusedError:
                    await asyncio.sleep(.01)
            writer.write(b'original-bytes\x00\xff')
            await writer.drain()
            assert await asyncio.wait_for(reader.read(100), 3) == b'original-bytes\x00\xff'
            writer.close()
            await writer.wait_closed()
        finally:
            proxy.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await proxy
            service.close()
            await service.wait_closed()

    asyncio.run(run())


def test_repeated_bridge_readiness_checks_do_not_connect_to_mail_service():
    module = bridge_module()

    async def run():
        connections = []

        async def smtp(reader, writer):
            connections.append(True)
            writer.close()
            await writer.wait_closed()

        service = await asyncio.start_server(smtp, '127.0.0.2', 0)
        port = service.sockets[0].getsockname()[1]
        proxy = asyncio.create_task(module.forward([port], destination='127.0.0.2'))
        try:
            await asyncio.to_thread(module.wait_for_listeners, [port], timeout=1)
            for _ in range(108):
                module.wait_for_listeners([port], timeout=1)
            await asyncio.sleep(.05)
            assert not connections
        finally:
            proxy.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await proxy
            service.close()
            await service.wait_closed()

    asyncio.run(run())


def test_bridge_readiness_rejects_a_port_without_a_listener():
    import socket
    module = bridge_module()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        with pytest.raises(RuntimeError, match='bridge did not start'):
            module.wait_for_listeners([reserved.getsockname()[1]], timeout=0)


@pytest.mark.asyncio
async def test_run_bridge_signals_ready_after_binding_without_connecting_upstream(tmp_path):
    import socket
    module = bridge_module()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    ready = tmp_path / 'ready'
    proxy = asyncio.create_task(module.forward([port], destination='127.0.0.2', ready_file=ready))
    try:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(.01)
        assert ready.exists()
        module.wait_for_listeners([port], timeout=0)
        assert not proxy.done()
    finally:
        proxy.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await proxy


def test_job_service_bridge_waits_once_before_tasks_and_stops_afterwards(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = bridge_module()
    monkeypatch.setenv('TOOLATHLON_TASK_NETWORK', 'toolathlon-tasks')
    monkeypatch.setattr(module, 'service_ports', lambda root: [2143, 2587])
    calls = []
    markers = []
    def spawn(args, **kwargs):
        ready = Path(args[-1])
        assert not ready.exists()
        markers.append(ready)
        calls.append('start')
        return SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append('stop'),
                               wait=lambda **kwargs: calls.append('wait'))
    def startup(seconds):
        assert calls == ['start']
        # The real bridge writes this only after it has bound every port.
        markers[0].touch()
        calls.append('ready')
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(module.time, 'sleep', startup)
    with module.job_service_bridge(tmp_path):
        assert calls == ['start', 'ready']
        for _ in range(108):
            calls.append('task')
    assert calls.count('start') == 1
    assert calls[-2:] == ['stop', 'wait']


def test_job_bridge_startup_failure_does_not_launch_tasks(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = bridge_module()
    monkeypatch.setenv('TOOLATHLON_TASK_NETWORK', 'toolathlon-tasks')
    monkeypatch.setattr(module, 'service_ports', lambda root: [2143])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(poll=lambda: 1))
    with pytest.raises(RuntimeError, match='exited during startup'):
        with module.job_service_bridge(tmp_path):
            pytest.fail('tasks started before their bridge was ready')


@pytest.mark.asyncio
async def test_delayed_gateway_start_is_ready_before_task_connections(tmp_path, monkeypatch):
    import os
    module = bridge_module()
    connections = []
    async def service(reader, writer):
        connections.append(True)
        writer.write(b'* OK IMAP ready\r\n')
        await writer.drain()
        await reader.read(100)
        writer.close()
        await writer.wait_closed()
    upstream = await asyncio.start_server(service, '127.0.0.1', 0)
    port = upstream.sockets[0].getsockname()[1]
    config = tmp_path / 'global_preparation/deploy_containers.sh'
    config.parent.mkdir()
    names = ('CANVAS_HTTP_PORT', 'CANVAS_HTTPS_PORT', 'POSTE_WEB_PORT', 'POSTE_SMTP_PORT',
             'POSTE_IMAP_PORT', 'POSTE_SUB_PORT', 'WOO_PORT')
    config.write_text(''.join(f'{name}={port}\n' for name in names))
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\nsleep 0.3\nprintf \'[{"IPAM":{"Config":[{"Gateway":"127.0.0.2"}]}}]\\n\'\n')
    docker.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('TOOLATHLON_TASK_NETWORK', 'toolathlon-tasks')
    proxy = None
    try:
        with module.job_service_bridge(tmp_path):
            assert connections == []  # Startup did not probe the mail service.
            proxy = asyncio.create_task(module.forward([port], bind='127.0.0.3', destination='127.0.0.2'))
            for _ in range(100):
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.3', port)
                    break
                except ConnectionRefusedError:
                    await asyncio.sleep(.01)
            assert await asyncio.wait_for(reader.readline(), 2) == b'* OK IMAP ready\r\n'
            writer.write(b'a LOGOUT\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            assert len(connections) == 1
    finally:
        if proxy:
            proxy.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await proxy
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_mysql_diagnostics_keep_first_failure_and_exclude_pod_credentials(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('mysql_diagnostics', Path(__file__).resolve().parents[1] / 'scripts/platform/run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rounds = [0]
    async def command(*args):
        if args[1] == 'ps':
            return b'cluster-mysql-control-plane\n'
        if 'get' in args:
            return json.dumps({'status': {'phase': 'Running'}, 'spec': {'private_key': 'never-log'}}).encode()
        return b'first initialization error' if rounds[0] == 0 or '--previous' in args else b'data directory is not empty'
    async def sleep(seconds):
        rounds[0] += 1
        if rounds[0] == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(module, 'command', command)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    output = tmp_path / 'runtime_logs/k8s/mysql-startup.log'
    with pytest.raises(asyncio.CancelledError):
        await module.capture_mysql_diagnostics(output)
    text = output.read_text()
    assert 'first initialization error' in text and 'data directory is not empty' in text
    assert 'never-log' not in text and 'private_key' not in text


@pytest.mark.asyncio
async def test_callback_keeps_task_auth_and_rejects_other_run(monkeypatch):
    spec = importlib.util.spec_from_file_location('platform_run', Path(__file__).resolve().parents[1] / 'scripts/platform/run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('MCPHUB_RUN_ID', 'our-run')
    seen = []

    async def task(request):
        seen.append((request.headers.get('Authorization'), await request.read()))
        return web.Response(body=b'{"result":"task-result"}', content_type='application/json')

    backend = web.Application()
    backend.router.add_post('/bridge/{binding}/call-tool', task)
    async with TestServer(backend, host='127.0.0.1') as target:
        owner = 'our-run'

        async def inspect(*args):
            return json.dumps([{'Config': {'Labels': {'toolathlon.hub-run': owner}},
                                'NetworkSettings': {'Networks': {'toolathlon-tasks': {'IPAddress': '127.0.0.1'}}}}]).encode()

        monkeypatch.setattr(module, 'command', inspect)
        app = web.Application()
        async with module.ClientSession() as upstream:
            app['client'] = upstream
            app.router.add_post('/callbacks/{container}/{port}/{path:.*}', module.callback)
            async with TestClient(TestServer(app)) as client:
                route = '/callbacks/' + 'a' * 12 + f'/{target.port}/bridge/' + 'b' * 32 + '/call-tool'
                response = await client.post(route, data=b'{"name":"read"}', headers={'Authorization': 'Bearer task-binding'})
                assert response.status == 200
                assert await response.json() == {'result': 'task-result'}
                assert seen == [('Bearer task-binding', b'{"name":"read"}')]
                owner = 'another-run'
                response = await client.post(route)
                assert response.status == 403
                assert len(seen) == 1
