"""Local execution deadlines must not include time waiting in Hub admission."""
import ast
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_n_run_repetitions import scheduler_class
from scripts.task_retry import deferred_retry_reason

ROOT = Path(__file__).resolve().parents[1]


def initialize_local_server(hub, name, timeout=120):
    tree = ast.parse((ROOT/'utils/mcp/tool_servers.py').read_text())
    manager = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MCPServerManager')
    method = next(n for n in manager.body if isinstance(n,ast.FunctionDef) and n.name=='_initialize_server_from_config')
    captured = {}
    def backend(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)
    scope = dict(Dict=dict, Any=object, MCPServerStdio=backend, MCPServerSse=backend,
                 HubTaskServer=lambda name, server, bridge, config:server,
                 os=SimpleNamespace(geteuid=lambda:1000))
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(ROOT/'utils/mcp/tool_servers.py'),'exec'),scope)
    instance=SimpleNamespace(hub_settings=SimpleNamespace(shared=set()) if hub else None,
                             hub_bridge=object() if hub else None,servers={},
                             _process_config_params=lambda p:p)
    scope['_initialize_server_from_config'](instance,dict(type='stdio',name=name,
        params=dict(command='unused',args=[]),client_session_timeout_seconds=timeout),name)
    return captured


@pytest.mark.parametrize('name,timeout', [
    ('playwright_with_chunk', 120), ('k8s', 600), ('github', 100),
    ('fetch', 100), ('notion', 120),
])
def test_task_backends_preserve_native_execution_deadlines_in_both_modes(name, timeout):
    assert initialize_local_server(True, name, timeout)['client_session_timeout_seconds'] == timeout
    assert initialize_local_server(False, name, timeout)['client_session_timeout_seconds'] == timeout


def test_container_keepalive_has_no_fixed_task_budget_and_can_be_stopped():
    script=(ROOT/'scripts/run_single_containerized.sh').read_text()
    tail=script.split('    # Set image\n',1)[1].split(')\n',1)[0]
    command=tail.split('"$IMAGE_NAME"',1)[1]
    tokens=__import__('shlex').split(command,comments=True)
    process=subprocess.Popen(tokens)
    try:
        assert process.poll() is None
        process.terminate()
        assert process.wait(timeout=2)!=0
    finally:
        if process.poll() is None:
            process.kill();process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['running','timeout','done'])
async def test_abnormal_exit_does_not_leave_running_or_relabel_137_as_timeout(tmp_path,monkeypatch,state):
    scheduler=scheduler_class()(None,1)
    directory=tmp_path/'run1/finalpool/task'
    async def execute(command,log_file,**kwargs):
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'status.json').write_text(json.dumps(dict(preprocess='done',running=state,evaluation=None)))
        return dict(success=False,returncode=137)
    monkeypatch.setitem(scheduler._execute_task.__globals__,'run_command_async',execute)
    outcome=await scheduler.run_single_task('finalpool/task','full','model','unified','200',dump_path=str(tmp_path/'run1'))
    saved=json.loads((directory/'status.json').read_text())
    assert outcome['status']=='failed'
    assert saved['running']==('fail' if state=='running' else state)
    assert deferred_retry_reason(directory)==(None if state=='timeout' else 'environment_or_evaluation_incomplete')
