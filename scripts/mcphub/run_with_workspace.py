#!/usr/bin/env python3
"""Run existing Toolathlon commands against one Hub-owned account workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.mcp.workspace_credentials import (CredentialError, WorkspaceClient, OAuthSync,
                                              materialize, snapshot, write_private)


def terminate(process):
    # Keep the saved process group even if its leader exits during cleanup.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def stop_task_containers(root, run_id):
    """Never release an account while one of its labelled task containers runs."""
    marker = root/'configs/.hub-container-runtime'
    if not marker.is_file(): return
    runtime = marker.read_text().strip()
    if runtime not in {'docker', 'podman'}:
        raise CredentialError('Unknown task container runtime', code='cleanup_failed')
    def command(*args):
        result = subprocess.run([runtime, *args], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise CredentialError('Task container cleanup could not be confirmed', code='cleanup_failed')
        return result.stdout.split()
    selected = command('ps', '-q', '--filter', 'label=toolathlon.hub-run='+run_id)
    if selected:
        command('stop', '--time', '10', *selected)
    if command('ps', '-q', '--filter', 'label=toolathlon.hub-run='+run_id):
        raise CredentialError('Task containers are still running', code='cleanup_failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/share/toolathlon-hub-runs')
    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='Existing command after --, e.g. python scripts/n_run.py --N 1 --model_name MODEL ...')
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('Provide a Toolathlon command after --')
    client = WorkspaceClient.from_env()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = Path(tempfile.mkdtemp(prefix=client.catalog+'-'+client.workspace+'-', dir=args.state_dir))
    stop = threading.Event()
    failures = []
    process = None
    acquired = False
    sync = None
    worker = None
    exit_reason = 'startup_failed'
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    checkpoint_request = None
    checkpoint_ack = None
    def record(state, error=None):
        # Metadata only: never persist capabilities, credentials or exception text.
        try:
            write_private(run_root/'workspace-status.json', json.dumps({
                'runId': client.id, 'catalog': client.catalog, 'workspace': client.workspace,
                'state': state, 'error': error, 'updatedAt': time.time(),
            }))
        except OSError:
            print('Could not save local run status metadata.', file=sys.stderr)
    def maintain():
        last_renewal = time.monotonic()
        acknowledged = ''
        while not stop.wait(.5):
            try:
                if time.monotonic() - last_renewal >= 10:
                    client.renew()
                    synchronized = sync.sync()
                    if synchronized: client.report(synced=True)
                    record('running')
                    last_renewal = time.monotonic()
                if checkpoint_request.is_file():
                    request = json.loads(checkpoint_request.read_text())
                    nonce = request.get('nonce')
                    if isinstance(nonce, str) and nonce and nonce != acknowledged:
                        if sync.sync(final=True):
                            write_private(checkpoint_ack, json.dumps({'nonce': nonce}))
                            acknowledged = nonce
            except Exception as exc:
                failure = getattr(exc, 'code', 'sync_failed')
                failures.append('Hub account lease or OAuth synchronization failed')
                record('recovery_required', failure)
                try: client.report(error=failure)
                except Exception: pass
                return
    try:
        client.start()
        acquired = True
        credentials = client.credentials()
        snapshot(ROOT, run_root)
        materialize(run_root, credentials)
        from utils.mcp.hub_instance import reconcile_mail_ports
        repaired = reconcile_mail_ports(run_root)
        if repaired:
            print(f'Applied declared local mail ports in run configuration: {repaired} fields', flush=True)
        sync = OAuthSync(client, run_root, credentials['files'])
        checkpoint_request = run_root/'configs/.hub-sync-request.json'
        checkpoint_ack = run_root/'configs/.hub-sync-ack.json'
        env = {**os.environ, 'TOOLATHLON_HUB_CHECKPOINT_FILE': str(checkpoint_request), 'MCP_TOOL_TRANSPORT': 'hub', 'MCPHUB_RUN_ID': client.id,
               'MCPHUB_RUN_TOKEN': client.token, 'TOOLATHLON_HUB_SNAPSHOT': '1',
               'TOOLATHLON_SKIP_GLOBAL_PRUNE': '1'}
        # Relative paths in source configs retain their original meaning in the snapshot.
        env.pop('PYTHONPATH', None)
        print(f'Hub workspace: {client.catalog}/{client.workspace}; run: {client.id}', flush=True)
        print(f'Private run directory: {run_root}', flush=True)
        print('This command holds the account until it exits. In an interactive workspace terminal, finishing or cancelling an evaluation keeps the account reserved; type exit to synchronize and release it.', flush=True)
        record('running')
        worker = threading.Thread(target=maintain, daemon=True)
        worker.start()
        process = subprocess.Popen(command, cwd=run_root, env=env, start_new_session=True)
        while process.poll() is None:
            if failures:
                raise CredentialError(failures[0])
            time.sleep(.5)
        exit_reason = 'completed' if process.returncode == 0 else 'cancelled' if process.returncode in {130, -signal.SIGINT, -signal.SIGTERM, 143} else 'command_failed'
        return process.returncode
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('Workspace run failed. Check Hub availability, workspace configuration and local prerequisites; secrets were not printed.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        exit_reason = 'cancelled'
        return 130
    finally:
        # A second Ctrl+C must not interrupt token persistence/account release.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        cleanup_failed = False
        if process:
            try: terminate(process)
            except OSError: cleanup_failed = True
        if acquired:
            try: stop_task_containers(run_root, client.id)
            except (CredentialError, OSError, subprocess.TimeoutExpired): cleanup_failed = True
        stop.set()
        if worker:
            worker.join(timeout=40)
        if acquired:
            try:
                if cleanup_failed:
                    raise CredentialError('Process cleanup failed')
                if worker and worker.is_alive():
                    raise CredentialError('Account synchronization is still running')
                if sync and not sync.sync(final=True):
                    raise CredentialError('OAuth state is still in use')
                client.report(synced=True)
                client.finish(exit_reason)
                record('released')
                print('Hub authorization synchronized; workspace account released.', flush=True)
            except Exception as exc:
                failure = 'cleanup_failed' if cleanup_failed else getattr(exc, 'code', 'sync_failed')
                record('recovery_required', failure)
                try: client.report(error=failure)
                except Exception: pass
                # Keep snapshot for recovery. Do not overwrite or release newer state.
                print(f'Account state was not fully synchronized/released; retain {run_root} and inspect before reuse.', file=sys.stderr)
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)
                return 1
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        # Results and unsynchronized OAuth material remain private in this directory.
        # A recovery directory is preferable to silently losing rotated refresh tokens.


if __name__ == '__main__':
    sys.exit(main())
