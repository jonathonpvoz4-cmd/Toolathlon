"""Run Toolathlon inside one private, bridge-attached evaluation container.

The Docker socket in this container belongs to its own dockerd. No deployment,
port cleanup or Kind command is sent to the worker's Docker socket.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from datetime import datetime, timezone

from aiohttp import ClientSession, ClientTimeout, web


async def command(*args):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError(f'{args[0]} failed')
        return out
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def capture_mysql_diagnostics(output):
    """Retain the first MySQL startup failure before kubelet rotates its logs."""
    previous = {}
    while True:
        try:
            nodes = (await asyncio.wait_for(command(
                'docker', 'ps', '--filter', 'name=cluster-mysql',
                '--filter', 'label=io.x-k8s.kind.role=control-plane', '--format', '{{.Names}}'), 10)).decode().splitlines()
            for node in nodes:
                base = ('docker', 'exec', node, 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '-n', 'data')
                for mode, args in [('current', ('logs', 'mysql-f1-0', '--tail=200')),
                                   ('previous', ('logs', 'mysql-f1-0', '--previous', '--tail=200')),
                                   ('status', ('get', 'pod', 'mysql-f1-0', '-o', 'json'))]:
                    try:
                        data = await asyncio.wait_for(command(*base, *args), 10)
                        if mode == 'status':
                            data = json.dumps(json.loads(data).get('status', {}), sort_keys=True).encode()
                        key = (node, mode)
                        if data and previous.get(key) != data:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with output.open('a', encoding='utf-8') as handle:
                                handle.write(f'\n{datetime.now(timezone.utc).isoformat()} {node} {mode}\n')
                                handle.write(data.decode(errors='replace') + '\n')
                            previous[key] = data
                    except (RuntimeError, asyncio.TimeoutError, ValueError):
                        pass
        except (RuntimeError, asyncio.TimeoutError, OSError):
            pass
        await asyncio.sleep(2)


async def callback(request):
    container = request.match_info['container']
    port = int(request.match_info['port'])
    if not re.fullmatch(r'[a-f0-9]{12,64}', container) or not 1024 <= port <= 65535:
        raise web.HTTPNotFound()
    records = json.loads(await command('docker', 'inspect', container))
    record = records[0]
    if record['Config'].get('Labels', {}).get('toolathlon.hub-run') != os.environ.get('MCPHUB_RUN_ID'):
        raise web.HTTPForbidden()
    ip = record['NetworkSettings']['Networks'].get('toolathlon-tasks', {}).get('IPAddress')
    if not ip:
        raise web.HTTPNotFound()
    path = request.match_info['path']
    if not re.fullmatch(r'bridge/[a-f0-9]{32}/(?:list-tools|call-tool)', path):
        raise web.HTTPNotFound()
    async with request.app['client'].post(
        f'http://{ip}:{port}/{path}', data=request.content,
        headers={'Authorization': request.headers.get('Authorization', ''),
                 'Content-Type': request.headers.get('Content-Type', 'application/json')},
    ) as upstream:
        response = web.StreamResponse(status=upstream.status,
                                      headers={'Content-Type': upstream.headers.get('Content-Type', 'application/json')})
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(65536):
            await response.write(chunk)
        await response.write_eof()
        return response


async def main():
    out = Path('/out')
    assets = out / 'runtime_assets'
    options = json.loads((assets / 'toolathlon.json').read_text())
    for _ in range(60):
        if (assets / 'callback.json').exists():
            break
        await asyncio.sleep(.5)
    callback_url = json.loads((assets / 'callback.json').read_text())['url']
    # Enable localhost port forwarding from sibling task bridges to this job.
    daemon = subprocess.Popen(['dind', 'dockerd', '--host=unix:///var/run/docker.sock',
                               '--data-root=/var/lib/docker', '--storage-driver=fuse-overlayfs',
                               '--iptables=true'],
                              stdout=open(os.environ.get('TOOLATHLON_DOCKERD_LOG_PATH', '/tmp/dockerd.log'), 'a'),
                              stderr=subprocess.STDOUT)
    app = web.Application(client_max_size=0)
    app['client'] = ClientSession(timeout=ClientTimeout(total=None))
    app.router.add_post('/callbacks/{container}/{port}/{path:.*}', callback)
    server = web.AppRunner(app, access_log=None, handler_cancellation=True)
    child = None
    diagnostics = None
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        for _ in range(120):
            try:
                await command('docker', 'info', '--format', '{{.ID}}')
                break
            except RuntimeError:
                if daemon.poll() is not None:
                    raise RuntimeError('Private Docker daemon failed; inspect runtime log')
                await asyncio.sleep(1)
        else:
            raise RuntimeError('Private Docker daemon did not become ready')
        (assets / 'daemon-ready').touch()
        diagnostics = asyncio.create_task(capture_mysql_diagnostics(out / 'runtime_logs/k8s/mysql-startup.log'))
        while not (assets / 'images-ready').exists():
            if stop.is_set():
                return 130
            await asyncio.sleep(.5)
        await command('docker', 'network', 'create', 'toolathlon-tasks')
        await server.setup()
        await web.TCPSite(server, '0.0.0.0', 19840).start()
        env = {**os.environ, 'TOOLATHLON_TASK_NETWORK': 'toolathlon-tasks',
               'TOOLATHLON_CALLBACK_BASE_URL': callback_url,
               'TOOLATHLON_SKIP_GLOBAL_PRUNE': '1'}
        # Use the benchmark's known schema; model/runtime defaults are supplied
        # by its original configuration, never by model-specific example files.
        original = json.loads(Path('scripts/formal_run_v0.json').read_text())
        original['agent']['model'] = {'short_name': options['model'], 'provider': 'unified'}
        original['agent']['generation']['max_tokens'] = 262144
        original['agent']['generation'].update(options.get('generation', {}))
        # The generated model configuration travels with the run snapshot.
        config_path = Path('scripts/platform-model.json')
        config_path.write_text(json.dumps(original))
        config_path.chmod(0o600)
        task_list = Path('scripts/platform-tasks.txt')
        task_list.write_text('\n'.join(options['tasks']) + '\n')
        args = ['.venv/bin/python', 'scripts/mcphub/run_with_workspace.py', '--state-dir', '/private',
                '--', '.venv/bin/python', 'scripts/platform/single_run.py',
                '--model', options['model'], '--output', '/out/results',
                '--workers', str(options['concurrency']), '--config', str(config_path),
                '--repetitions', str(options.get('repetitions', 1)), '--task-list', str(task_list.resolve()),
                '--max-steps', str(options.get('max_steps', 200))]
        (assets / 'workspace-started').touch()
        child = await asyncio.create_subprocess_exec(*args, env=env, start_new_session=True)
        waiter = asyncio.create_task(child.wait())
        cancelled = asyncio.create_task(stop.wait())
        await asyncio.wait([waiter, cancelled], return_when=asyncio.FIRST_COMPLETED)
        if not waiter.done():
            os.killpg(child.pid, signal.SIGTERM)
        result = await child.wait()
        statuses = list(Path('/private').glob('*/workspace-status.json'))
        if statuses and all(json.loads(path.read_text()).get('state') == 'released' for path in statuses):
            (assets / 'account-released').touch()
        return result
    finally:
        if child and child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            await child.wait()
        if diagnostics:
            diagnostics.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await diagnostics
        await server.cleanup()
        await app['client'].close()
        daemon.terminate()
        await asyncio.to_thread(daemon.wait)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
