"""Shutdown cannot prevent evaluation after an isolated agent saves its result."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', ['wait', 'cancel_resistant', 'shutdown', 'spin'])
@pytest.mark.parametrize('exit_code', [0, 1])
def test_saved_result_survives_stuck_shutdown_and_process_exits(tmp_path, mode, exit_code):
    trajectory = tmp_path / 'traj_log.json'
    code = f'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(ROOT)!r})
from utils.general.agent_cleanup import AgentCleanupWatchdog
watchdog = AgentCleanupWatchdog(timeout=.15)
async def stuck():
    while True:
        try: await asyncio.Event().wait()
        except asyncio.CancelledError: pass
async def agent():
    child = asyncio.create_task(stuck())
    await asyncio.sleep(0)
    Path({str(trajectory)!r}).write_text(json.dumps({{"status": {('success' if exit_code == 0 else 'interrupted')!r}, "messages": ["saved-answer"]}}))
    watchdog.arm({exit_code}, SimpleNamespace(_server_tasks={{"huggingface": child}}))
    if {mode!r} == "shutdown": return
    if {mode!r} == "spin":
        while True: pass
    if {mode!r} == "cancel_resistant":
        try: await asyncio.wait_for(child, .01)
        except asyncio.TimeoutError: pass
    await asyncio.Event().wait()
asyncio.run(agent())
'''
    result = subprocess.run([sys.executable, '-P', '-c', code], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == exit_code, result.stderr
    assert json.loads(trajectory.read_text())['messages'] == ['saved-answer']
    assert 'pending MCP servers: huggingface' in result.stderr
    assert 'exiting isolated agent so evaluation can continue' in result.stderr


def test_unarmed_and_cancelled_watchdog_do_not_exit_process():
    code = f'''
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from utils.general.agent_cleanup import AgentCleanupWatchdog
watchdog = AgentCleanupWatchdog(timeout=.05)
time.sleep(.1)
watchdog.arm(1, None)
watchdog.cancel()
time.sleep(.1)
print("normal shutdown")
'''
    result = subprocess.run([sys.executable, '-P', '-c', code], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'normal shutdown'


def test_agent_shutdown_exit_continues_to_trusted_evaluation(tmp_path):
    script = (ROOT / 'scripts/run_single_containerized.sh').read_text()
    block = script.split('    echo "Step 3.3: Running prepared agent..."', 1)[1].split(
        '\nfi\n\necho ""', 1)[0]
    child = tmp_path / 'agent.py'
    child.write_text(f'''
import asyncio,sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from utils.general.agent_cleanup import AgentCleanupWatchdog
async def main():
    Path({str(tmp_path / 'traj_log.json')!r}).write_text('{{"status":"success","messages":["answer"]}}')
    AgentCleanupWatchdog(.1).arm(0, None)
    await asyncio.Event().wait()
asyncio.run(main())
''')
    header = f'''set -e
CONTAINER_RUNTIME=runtime; CONTAINER_NAME=task; CONTAINER_TASK_ROOT=out
CONTAINER_TASK_PATH=task; CURRENT_CONTAINER_BUNDLE=bundle; ARTIFACT_STASH_DIR=stash
stage_trusted_bundle() {{ :; }}
discard_container_bundle() {{ :; }}
runtime() {{ echo "runtime $*" >> {str(tmp_path / 'events')!r}; }}
uv() {{ echo "trusted restore" >> {str(tmp_path / 'events')!r}; }}
run_container_phase() {{
  if [[ "$*" == *container_agent* ]]; then
    {sys.executable} -P {str(child)!r}
  else
    [[ "$*" == *"--agent_exit_code 0"* ]] || return 19
    [[ -s {str(tmp_path / 'traj_log.json')!r} ]] || return 20
    echo "evaluation" >> {str(tmp_path / 'events')!r}
  fi
}}
'''
    result = subprocess.run(['bash', '-c', header + block + '\nexit "$EXEC_EXIT_CODE"'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    events = (tmp_path / 'events').read_text()
    assert events.index('trusted restore') < events.index('evaluation')
    assert 'docker stop' not in events
    assert 'exiting isolated agent so evaluation can continue' in result.stderr
