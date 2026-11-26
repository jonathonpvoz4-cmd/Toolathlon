"""Offline checks of the native repetition driver and real conflict/semaphore code."""
import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import List, Optional
import sys
import traceback
import shutil
from datetime import datetime

import pytest

ROOT = Path(__file__).resolve().parents[1]


def driver():
    spec = importlib.util.spec_from_file_location('native_n_run', ROOT / 'scripts/n_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_timeout_is_retained_without_claiming_it_was_graded(tmp_path):
    module = driver()
    directory = tmp_path / 'run1/finalpool/task'
    directory.mkdir(parents=True)
    (directory / 'status.json').write_text(json.dumps({
        'preprocess': 'done', 'running': 'timeout', 'evaluation': None,
    }))
    module.retain_timeout_result(directory)
    score = json.loads((directory / 'eval_res.json').read_text())
    assert score == {'pass': None, 'details': 'Execution budget exhausted; no evaluator result available',
                     'outcome': 'ungraded_timeout'}
    assert module.has_terminal_result(directory)
    module.retain_timeout_result(directory)


def test_preprocess_failure_is_still_resumable(tmp_path):
    module = driver()
    (tmp_path / 'status.json').write_text(json.dumps({'preprocess': 'fail', 'running': None}))
    module.retain_timeout_result(tmp_path)
    assert not (tmp_path / 'eval_res.json').exists()
    assert not module.has_terminal_result(tmp_path)


def scheduler_class():
    # Execute the native scheduling code without importing model SDK/config files.
    node = next(node for node in ast.parse((ROOT / 'run_parallel.py').read_text()).body
                if isinstance(node, ast.ClassDef) and node.name == 'AsyncTaskScheduler')
    namespace = dict(asyncio=asyncio, Optional=Optional, List=List, time=time, os=os,
                     TaskResult=SimpleNamespace, active_processes=set(),
                     datetime=datetime, traceback=traceback, Path=Path,
                     read_json=lambda path: json.loads(Path(path).read_text()), shutil=shutil)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<native scheduler>', 'exec'), namespace)
    return namespace['AsyncTaskScheduler']


def result(directory, passed=True, state='done', messages=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'status.json').write_text(json.dumps({
        'preprocess': 'done', 'running': state,
        'evaluation': passed if state == 'done' else None,
    }))
    (directory / 'eval_res.json').write_text(json.dumps({'pass': passed}))
    if messages is not None:
        (directory / 'traj_log.json').write_text(json.dumps({'status': 'failed', 'messages': messages}))


@pytest.mark.asyncio
async def test_slow_task_does_not_block_other_repetitions_and_conflicts_stay_serial(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()([['fast', 'conflicting']], 2)
    release_slow = asyncio.Event()
    active = set()
    events = []
    max_active = 0

    async def execute(task, *args, **kwargs):
        nonlocal max_active
        index = int(Path(kwargs['dump_path']).name[3:])
        assert task not in active
        if task in ('finalpool/fast', 'finalpool/conflicting'):
            assert not active.intersection({'finalpool/fast', 'finalpool/conflicting'})
        active.add(task)
        max_active = max(max_active, len(active))
        events.append((task, index, 'start'))
        try:
            if task == 'finalpool/slow' and index == 1:
                await release_slow.wait()
            await asyncio.sleep(0)
            result(Path(kwargs['dump_path']) / task)
            events.append((task, index, 'end'))
            if task == 'finalpool/fast' and index == 3:
                release_slow.set()
        finally:
            active.remove(task)
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    await asyncio.wait_for(module.execute_repetitions(
        ['slow', 'fast', 'conflicting'], 3, 'model', tmp_path, Path('config.json'), scheduler), 2)
    assert max_active == 2
    assert events.index(('finalpool/fast', 3, 'end')) < events.index(('finalpool/slow', 1, 'end'))
    assert len([event for event in events if event[2] == 'end']) == 9


@pytest.mark.asyncio
async def test_resume_preserves_failed_model_outcomes_per_repetition(tmp_path, monkeypatch, capsys):
    module = driver()
    result(tmp_path / 'run1/finalpool/task', False)
    result(tmp_path / 'run2/finalpool/task', None, 'fail', [{'role': 'user', 'content': 'task'}])
    scheduler = scheduler_class()(None, 2)
    calls = []
    async def execute(task, *args, **kwargs):
        calls.append((task, Path(kwargs['dump_path']).name))
        result(Path(kwargs['dump_path']) / task)
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    await module.execute_repetitions(['task'], 3, 'model', tmp_path, Path('config'), scheduler)
    assert calls == [('finalpool/task', 'run3')]
    assert ('completed=3/3 passed=1 incorrect=1 unknown=1 execution_failed=0 remaining=0'
            in capsys.readouterr().out)


@pytest.mark.asyncio
async def test_repetition_cancellation_stops_all_chains(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()(None, 2)
    started = asyncio.Event()
    stopped = []
    async def execute(task, *args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(task)
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    task = asyncio.create_task(module.execute_repetitions(['a', 'b'], 3, 'model', tmp_path, Path('config'), scheduler))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(stopped) == ['finalpool/a', 'finalpool/b']


@pytest.mark.asyncio
async def test_native_summary_averages_three_independent_scores(tmp_path, monkeypatch):
    module = driver()
    monkeypatch.setattr(module, 'REPO_ROOT', str(tmp_path))
    (tmp_path / 'task_files').mkdir()
    (tmp_path / 'task_files/all_tasks_full_108.txt').write_text('a\nb\n')
    (tmp_path / 'tasks/finalpool').mkdir(parents=True)
    (tmp_path / 'tasks/finalpool/task_conflict.json').write_text('{"conflict_groups": []}')
    scheduler = scheduler_class()
    async def execute(self, task, *args, **kwargs):
        index = int(Path(kwargs['dump_path']).name[3:])
        result(Path(kwargs['dump_path']) / task, index == 1 or (index == 3 and task.endswith('/a')))
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    monkeypatch.setitem(sys.modules, 'run_parallel', SimpleNamespace(AsyncTaskScheduler=scheduler))
    monkeypatch.setattr(module, 'summarize', lambda *args: None)
    await module.run_repetitions('model', tmp_path / 'out', 3, 2, Path('config'))
    summary = json.loads((tmp_path / 'out/repeat_summary.json').read_text())
    assert [round['pass_rate'] for round in summary['rounds']] == [1, 0, .5]
    assert summary['mean_pass_rate'] == .5


def test_native_task_list_selection_validates_before_execution(tmp_path, monkeypatch):
    module = driver()
    monkeypatch.setattr(module, 'REPO_ROOT', str(tmp_path))
    (tmp_path / 'task_files').mkdir()
    (tmp_path / 'task_files/all_tasks_full_108.txt').write_text('a\nb\nc\n')
    selected = tmp_path / 'selected.txt'
    selected.write_text('# comment\nb\na\nb\n')
    assert module.select_tasks(selected) == ['b', 'a']
    assert module.select_tasks() == ['a', 'b', 'c']
    selected.write_text('nonexistent\n')
    with pytest.raises(ValueError, match='unknown tasks'):
        module.select_tasks(selected)
    selected.write_text('')
    with pytest.raises(ValueError, match='empty'):
        module.select_tasks(selected)


def test_native_entry_deploys_once_and_defaults_to_three_repetitions(tmp_path, monkeypatch):
    module = driver()
    monkeypatch.setattr(module, 'REPO_ROOT', str(tmp_path))
    monkeypatch.setenv('MCP_TOOL_TRANSPORT', 'direct')
    monkeypatch.delenv('TASK_LIST', raising=False)
    (tmp_path / 'task_files').mkdir()
    (tmp_path / 'task_files/all_tasks_full_108.txt').write_text('task\n')
    config = tmp_path / 'config.json'
    config.write_text('{}')
    monkeypatch.setattr(sys, 'argv', ['n_run.py', '--model_name', 'test', '--result_output_path', str(tmp_path / 'out'), '--config-file', str(config)])
    commands = []
    monkeypatch.setattr(module, 'run_step', lambda command, log: commands.append(command) or 0)
    calls = []
    async def run(*args):
        calls.append(args)
    async def entry(evaluate):
        await evaluate()
        return 0
    monkeypatch.setattr(module, 'run_repetitions', run)
    monkeypatch.setitem(sys.modules, 'run_parallel', SimpleNamespace(main_with_signal_handling=entry))
    assert module.main() == 0
    assert len(commands) == 2
    assert 'scripts/bootstrap_notion_official.sh' in commands[0]
    assert 'global_preparation/deploy_containers.sh' in commands[1]
    assert len(calls) == 1 and calls[0][2] == 3
    assert calls[0][3] == 32


@pytest.mark.asyncio
@pytest.mark.parametrize('absolute', [False, True])
async def test_repetitions_pass_config_under_task_workspace(tmp_path, monkeypatch, absolute):
    module = driver()
    snapshot = tmp_path / 'private/account-a'
    relative = Path('scripts/platform-model.json')
    config = snapshot / relative
    config.parent.mkdir(parents=True)
    config.write_text('{"agent":{"model":{"short_name":"test-model"}}}')
    task_workspace = tmp_path / 'task-container/workspace'
    (task_workspace / relative).parent.mkdir(parents=True)
    (task_workspace / relative).write_bytes(config.read_bytes())
    monkeypatch.setattr(module, 'REPO_ROOT', str(snapshot))
    calls = []

    async def execute(task, *args, **kwargs):
        received = Path(kwargs['eval_config'])
        assert not received.is_absolute()
        assert json.loads((task_workspace / received).read_text()) == json.loads(config.read_text())
        calls.append(Path(kwargs['dump_path']).name)
        result(Path(kwargs['dump_path']) / task)

    scheduler = scheduler_class()(None, 1)
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    await module.execute_repetitions(['task'], 3, 'model', tmp_path / 'out',
                                     config if absolute else relative, scheduler)
    assert calls == ['run1', 'run2', 'run3']


@pytest.mark.asyncio
async def test_failed_preprocess_is_collected_without_blocking_later_repetitions(tmp_path, monkeypatch, capsys):
    module = driver()
    scheduler = scheduler_class()(None, 1)
    commands = []

    async def failed(command, log_file, **kwargs):
        commands.append(command)
        return {'success': False, 'returncode': 1, 'log_file': log_file}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', failed)
    await module.execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler)
    printed = capsys.readouterr().out
    assert 'FAILED: finalpool/task' in printed
    assert 'Task process exited with code: 1' in printed
    assert 'SUCCESS:' not in printed
    assert '[Repeat queued 2/3]' in printed
    assert 'completed=0/3 passed=0 incorrect=0 unknown=0 execution_failed=3 remaining=0' in printed
    assert len(commands) == 6
    assert all('_retry' not in c for c in commands[:3])
    assert all('_retry' in c for c in commands[3:])
    assert scheduler.failed_tasks == 6
    assert scheduler.completed_tasks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('passed,state,messages', [
    (False, 'done', None), (None, 'fail', [{'role': 'user', 'content': 'task'}]),
])
async def test_scored_model_failure_still_runs_all_repetitions(tmp_path, monkeypatch, capsys, passed, state, messages):
    module = driver()
    scheduler = scheduler_class()(None, 1)
    calls = []

    async def failed_model(command, log_file, **kwargs):
        calls.append(command)
        result(Path(log_file).parent, passed, state, messages)
        return {'success': False, 'returncode': 1, 'log_file': log_file}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', failed_model)
    await module.execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler)
    assert len(calls) == 3
    assert scheduler.failed_tasks == 0
    assert scheduler.incorrect_tasks == (3 if passed is False else 0)
    assert 'FAILED:' not in capsys.readouterr().out
    assert all(module.has_terminal_result(tmp_path / f'run{index}/finalpool/task')
               for index in range(1, 4))
