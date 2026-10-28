"""Ask the run's sole OAuth synchronizer to persist refreshed authorization now."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from utils.mcp.workspace_credentials import CredentialError, write_private


def checkpoint(timeout=60):
    marker = os.getenv('TOOLATHLON_HUB_CHECKPOINT_FILE', '')
    if os.getenv('TOOLATHLON_HUB_SNAPSHOT') != '1' or not marker:
        raise CredentialError('Hub authorization checkpoint requires the workspace launcher')
    path = Path(marker)
    expected = Path.cwd()/'configs/.hub-sync-request.json'
    if path.resolve() != expected.resolve():
        raise CredentialError('Hub authorization checkpoint is outside the run directory')
    nonce = uuid.uuid4().hex
    write_private(path, json.dumps({'nonce': nonce}))
    ack = path.with_name('.hub-sync-ack.json')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if json.loads(ack.read_text()).get('nonce') == nonce:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise CredentialError('Hub did not confirm authorization persistence; preparation must stop')


if __name__ == '__main__':
    try:
        checkpoint()
        print('Hub authorization checkpoint saved')
    except CredentialError as exc:
        raise SystemExit(str(exc)) from None
