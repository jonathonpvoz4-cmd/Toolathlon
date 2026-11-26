"""Keep Toolathlon's local application endpoints within one evaluation."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import re
import time
import subprocess
import json
import logging
import os
import sys
import tempfile


def wait_for_listeners(ports: list[int], timeout=20):
    # Check the local forwarders without opening upstream SMTP connections.
    # Connect-and-close probes are counted as invalid clients by Poste.
    pending = set(ports)
    deadline = time.monotonic() + timeout
    while pending:
        listening = set()
        for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            fields = line.split()
            address, port = fields[1].split(':')
            if fields[3] == '0A' and address in {'0100007F', '00000000'}:
                listening.add(int(port, 16))
        pending -= listening
        if pending:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Task service bridge did not start: {sorted(pending)}')
            time.sleep(.1)


def service_ports(root: Path) -> list[int]:
    source = (root / 'global_preparation/deploy_containers.sh').read_text()
    names = ('CANVAS_HTTP_PORT', 'CANVAS_HTTPS_PORT', 'POSTE_WEB_PORT',
             'POSTE_SMTP_PORT', 'POSTE_IMAP_PORT', 'POSTE_SUB_PORT', 'WOO_PORT')
    ports = set()
    for name in names:
        match = re.search(rf'^{name}=(\d+)$', source, re.M)
        if not match:
            raise ValueError(f'Missing service port: {name}')
        ports.add(int(match[1]))
    for config in (root / 'deployment/k8s/configs').glob('*.yaml'):
        ports.update(int(port) for port in re.findall(r'https://(?:127\.0\.0\.1|localhost):(\d+)', config.read_text()))
    return sorted(ports)


async def forward(ports: list[int], *, bind='127.0.0.1', destination='toolathlon-job', allow_busy=False, ready_file=None):
    async def copy(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def handler(port):
        async def connect(reader, writer):
            try:
                remote, target = await asyncio.open_connection(destination, port)
                await asyncio.gather(copy(reader, target), copy(remote, writer))
            except (OSError, ConnectionError) as error:
                logging.warning('Service bridge connection failed: destination=%s port=%s error=%s',
                                destination, port, type(error).__name__)
                writer.close()
        return connect

    servers = []
    try:
        for port in ports:
            try:
                servers.append(await asyncio.start_server(handler(port), bind, port))
            except OSError as error:
                if not allow_busy or error.errno != 98:
                    raise
        if ready_file:
            Path(ready_file).touch()
        await asyncio.Event().wait()
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


@contextlib.contextmanager
def job_service_bridge(root: Path):
    """Own the Run's gateway forwarding after application ports are deployed."""
    if not os.getenv('TOOLATHLON_TASK_NETWORK'):
        yield
        return
    with tempfile.TemporaryDirectory(prefix='toolathlon-bridge-') as directory:
        ready = Path(directory) / 'ready'
        process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve()), '--job-bind',
            ','.join(map(str, service_ports(root))), '--ready-file', str(ready),
        ], cwd=root)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError('Run service bridge exited during startup')
                if time.monotonic() >= deadline:
                    raise RuntimeError('Run service bridge startup timed out')
                time.sleep(.05)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ports', action='store_true')
    parser.add_argument('--forward')
    parser.add_argument('--destination', default='toolathlon-job')
    parser.add_argument('--wait')
    parser.add_argument('--job-bind')
    parser.add_argument('--ready-file')
    args = parser.parse_args()
    if args.ports:
        print(','.join(map(str, service_ports(Path.cwd()))))
    elif args.forward is not None:
        asyncio.run(forward([int(p) for p in args.forward.split(',') if p], destination=args.destination))
    elif args.job_bind is not None:
        network = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'bridge']))[0]
        gateway = network['IPAM']['Config'][0]['Gateway']
        asyncio.run(forward([int(p) for p in args.job_bind.split(',') if p],
                            bind=gateway, destination='127.0.0.1', allow_busy=True,
                            ready_file=args.ready_file))
    elif args.wait is not None:
        ports = [int(p) for p in args.wait.split(',') if p]
        wait_for_listeners(ports)


if __name__ == '__main__':
    main()
