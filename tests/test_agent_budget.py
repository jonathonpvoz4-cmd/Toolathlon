import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from scripts.containerized.agent_budget import run_with_budget
from utils.api_model.interrupted_stream import agent_budget_state


@pytest.mark.asyncio
async def test_timeout_waits_for_agent_save_and_leaves_evaluation_alive(tmp_path):
    events = []

    async def agent():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(('expired', agent_budget_state.get()['expired']))
            raise
        finally:
            await asyncio.sleep(0.01)
            (tmp_path / 'traj_log.json').write_text('{"status":"interrupted"}')
            events.append('saved')

    with pytest.raises(asyncio.TimeoutError):
        await run_with_budget(agent(), 0.01)
    assert (tmp_path / 'traj_log.json').exists()
    events.append('evaluate')
    assert events == [('expired', True), 'saved', 'evaluate']


@pytest.mark.asyncio
async def test_user_cancellation_is_not_model_timeout():
    ready = asyncio.Event()
    expired = []

    async def agent():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            expired.append(agent_budget_state.get()['expired'])

    task = asyncio.create_task(run_with_budget(agent(), 60))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert expired == [False]


@pytest.mark.parametrize('failures,expected', [(0, 1), (1, 2), (2, 2)])
def test_only_failed_preprocess_is_retried_once(tmp_path, failures, expected):
    script = (Path(__file__).resolve().parents[1] / 'scripts/run_single_containerized.sh').read_text()
    block = script.split('    echo "Step 3.1: Running preprocess..."', 1)[1].split(
        '    # Move the trusted, resolved bundle off-container', 1)[0]
    header = f'''set -e
attempt=0
eval_config=config; task_dir_arg=task; maxstep=200; modelname=model; provider=unified
PREPROCESS_CONTAINER_BUNDLE=bundle
run_container_phase() {{
  attempt=$((attempt+1))
  echo "$attempt" >> "{tmp_path}/calls"
  [ "$attempt" -gt {failures} ]
}}
'''
    result = subprocess.run(['bash', '-c', header + block], capture_output=True, text=True)
    assert len((tmp_path / 'calls').read_text().splitlines()) == expected
    assert (result.returncode == 0) == (failures < 2)
