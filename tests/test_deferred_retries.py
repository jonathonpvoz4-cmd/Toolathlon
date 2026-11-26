"""Normal attempts drain before a bounded retry stage; checkpoints select one result."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_n_run_repetitions import driver, result, scheduler_class
from test_task_api_retry import api_failure
from scripts.task_retry import (
    claim_deferred_retry, deferred_retry_reason, has_terminal_result,
    retry_marker, selected_result,
)


def environment_failure(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'status.json').write_text('{"preprocess":"fail","running":null,"evaluation":null}')


@pytest.mark.asyncio
async def test_retries_wait_for_all_normal_attempts_and_keep_full_concurrency(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()([['a', 'b']], 2)
    normal_active = set()
    retry_active = set()
    normal_finished = []
    retries = []
    maximum_retry_active = 0
    release_slow = asyncio.Event()

    async def execute(command, log_file, **kwargs):
        nonlocal maximum_retry_active
        directory = Path(log_file).parent
        task, round_name = directory.name, directory.parent.parent.name
        if round_name.endswith('_retry'):
            assert not normal_active
            assert len(normal_finished) == 9
            retry_active.add(task)
            maximum_retry_active = max(maximum_retry_active, len(retry_active))
            await asyncio.sleep(.005)
            result(directory, task != 'b')
            retries.append((round_name, task))
            retry_active.remove(task)
        else:
            normal_active.add(task)
            if task == 'slow' and round_name == 'run1':
                await release_slow.wait()
            if task == 'a' and round_name == 'run3':
                release_slow.set()
            await asyncio.sleep(0)
            api_failure(directory)
            normal_finished.append((round_name, task))
            normal_active.remove(task)
        return {'success': round_name.endswith('_retry'), 'returncode': 0}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    await asyncio.wait_for(module.execute_repetitions(
        ['slow', 'a', 'b'], 3, 'model', tmp_path, Path('config.json'), scheduler), 3)
    assert normal_finished.index(('run3', 'a')) < normal_finished.index(('run1', 'slow'))
    assert len(retries) == 9
    assert maximum_retry_active == 2
    for index in range(1, 4):
        for task in ['slow', 'a', 'b']:
            original = tmp_path / f'run{index}/finalpool/{task}'
            selected = selected_result(original)
            assert selected == tmp_path / f'run{index}_retry/finalpool/{task}'
            assert json.loads((original / 'eval_res.json').read_text())['pass'] is None
            assert json.loads((selected / 'eval_res.json').read_text())['pass'] is (task != 'b')


@pytest.mark.asyncio
async def test_environment_failures_retry_after_all_normal_rounds(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()(None, 2)
    calls = []

    async def execute(command, log_file, **kwargs):
        directory = Path(log_file).parent
        round_name = directory.parent.parent.name
        calls.append(round_name)
        if round_name.endswith('_retry'):
            result(directory)
        else:
            environment_failure(directory)
        return {'success': round_name.endswith('_retry'), 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    await module.execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == ['run1', 'run2', 'run3', 'run1_retry', 'run2_retry', 'run3_retry']
    manifest = json.loads((tmp_path / 'repeat_results.json').read_text())
    assert len(manifest['results']) == 3
    assert all(row['completed'] for row in manifest['results'])


@pytest.mark.asyncio
async def test_api_failure_in_retry_is_terminal_and_resume_does_not_try_again(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()(None, 1)
    calls = []

    async def execute(command, log_file, **kwargs):
        directory = Path(log_file).parent
        calls.append(directory.parent.parent.name)
        api_failure(directory)
        return {'success': False, 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    for _ in range(2):
        await module.execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == ['run1', 'run2', 'run3', 'run1_retry', 'run2_retry', 'run3_retry']
    assert all(row['completed'] for row in json.loads((tmp_path / 'repeat_results.json').read_text())['results'])


@pytest.mark.asyncio
async def test_cancel_during_retry_resumes_retry_only(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()(None, 1)
    calls = []
    interrupt = True

    async def execute(command, log_file, **kwargs):
        nonlocal interrupt
        directory = Path(log_file).parent
        name = directory.parent.parent.name
        calls.append(name)
        if name.endswith('_retry'):
            if interrupt:
                interrupt = False
                raise asyncio.CancelledError()
            result(directory)
        else:
            api_failure(directory)
        return {'success': True, 'returncode': 0}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    with pytest.raises(asyncio.CancelledError):
        await module.execute_repetitions(['task'], 2, 'model', tmp_path, Path('config.json'), scheduler)
    assert retry_marker(tmp_path / 'run1/finalpool/task').exists()
    await module.execute_repetitions(['task'], 2, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == ['run1', 'run2', 'run1_retry', 'run1_retry', 'run2_retry']
    assert all(row['completed'] for row in json.loads((tmp_path / 'repeat_results.json').read_text())['results'])


@pytest.mark.asyncio
async def test_failed_retry_environment_remains_resumable_without_repeating_normal(tmp_path, monkeypatch):
    module = driver()
    scheduler = scheduler_class()(None, 1)
    calls = []
    recover = False

    async def execute(command, log_file, **kwargs):
        directory = Path(log_file).parent
        calls.append(directory.parent.parent.name)
        if recover:
            result(directory)
        else:
            environment_failure(directory)
        return {'success': recover, 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    await module.execute_repetitions(['task'], 1, 'model', tmp_path, Path('config.json'), scheduler)
    selected = selected_result(tmp_path / 'run1/finalpool/task')
    assert not has_terminal_result(selected, retry_exhausted=True)
    assert json.loads((tmp_path / 'repeat_results.json').read_text())['results'][0]['completed'] is False
    recover = True
    await module.execute_repetitions(['task'], 1, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == ['run1', 'run1_retry', 'run1_retry']


@pytest.mark.parametrize('state,passed', [('done', False), ('max_turn_exceeded', None), ('timeout', None)])
def test_model_outcomes_never_enter_deferred_retry(tmp_path, state, passed):
    result(tmp_path, passed, state)
    assert deferred_retry_reason(tmp_path) is None


@pytest.mark.asyncio
async def test_resume_retries_missing_grade_after_max_turns_once_in_separate_directory(tmp_path, monkeypatch):
    module = driver()
    directory = tmp_path / 'run1/finalpool/task'
    result(directory, None, 'max_turn_exceeded')
    (directory / 'eval_res.json').unlink()
    (directory / 'run.log').write_text('artifact guard error: failed to install certifi')
    (tmp_path / 'repeat_results.json').write_text(json.dumps({
        'schema_version': 1, 'repetitions': 1, 'normal_finished': ['run1/task'], 'results': [],
    }))
    assert not has_terminal_result(directory)
    assert deferred_retry_reason(directory) == 'environment_or_evaluation_incomplete'
    scheduler = scheduler_class()(None, 1)
    calls = []
    async def execute(command, log_file, **kwargs):
        destination = Path(log_file).parent
        calls.append(destination)
        result(destination, None, 'max_turn_exceeded')
        return {'success': False, 'returncode': 1}
    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    for _ in range(2):
        await module.execute_repetitions(['task'], 1, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == [tmp_path / 'run1_retry/finalpool/task']
    assert not (directory / 'eval_res.json').exists(), 'preserve the original failure evidence'
    row = json.loads((tmp_path / 'repeat_results.json').read_text())['results'][0]
    assert row['completed'] is True
    assert row['directory'] == 'run1_retry/finalpool/task'


def test_fatal_failure_is_not_automatically_retried(tmp_path):
    api_failure(tmp_path)
    (tmp_path / 'fatal_model_api.json').write_text('{"reason":"billing_exhausted"}')
    assert deferred_retry_reason(tmp_path) is None


def test_retry_claim_is_atomic_and_cannot_reset_its_budget(tmp_path):
    original = tmp_path / 'run1/finalpool/task'
    claim_deferred_retry(original, 'environment_or_evaluation_incomplete')
    with pytest.raises(FileExistsError):
        claim_deferred_retry(original, 'model_api_retries_exhausted')
    assert json.loads(retry_marker(original).read_text())['reason'] == 'environment_or_evaluation_incomplete'


@pytest.mark.asyncio
async def test_native_summary_selects_retry_once_and_preserves_original(tmp_path, monkeypatch):
    module = driver()
    (tmp_path / 'task_files').mkdir()
    (tmp_path / 'task_files/all_tasks_full_108.txt').write_text('task\n')
    (tmp_path / 'tasks/finalpool').mkdir(parents=True)
    (tmp_path / 'tasks/finalpool/task_conflict.json').write_text('{"conflict_groups": []}')
    config = tmp_path / 'config.json'
    config.write_text('{}')
    monkeypatch.setattr(module, 'REPO_ROOT', str(tmp_path))
    scheduler = scheduler_class()
    async def execute(self, task, *args, **kwargs):
        directory = Path(kwargs['dump_path']) / task
        if directory.parent.parent.name.endswith('_retry'):
            result(directory, False)
        else:
            api_failure(directory)
    monkeypatch.setattr(scheduler, '_execute_task', execute)
    monkeypatch.setitem(__import__('sys').modules, 'run_parallel', SimpleNamespace(AsyncTaskScheduler=scheduler))
    output = tmp_path / 'out'
    await module.run_repetitions('model', output, 3, 2, config)
    summary = json.loads((output / 'repeat_summary.json').read_text())
    assert summary['mean_pass_rate'] == 0
    assert [row['completed'] for row in summary['rounds']] == [1, 1, 1]
    for index in range(1, 4):
        original = output / f'run{index}/finalpool/task'
        assert json.loads((original / 'eval_res.json').read_text())['pass'] is None
        assert json.loads((output / f'run{index}/eval_res_all.jsonl').read_text())['pass'] is False
        assert json.loads((output / f'run{index}/eval_stats.json').read_text())['total_tasks'] == 1


@pytest.mark.asyncio
async def test_fatal_original_checkpoint_is_rerun_after_service_restored(tmp_path, monkeypatch):
    module = driver()
    directory = tmp_path / 'run1/finalpool/task'
    api_failure(directory)
    (directory / 'fatal_model_api.json').write_text('{"reason":"billing_exhausted"}')
    (tmp_path / 'repeat_results.json').write_text(json.dumps({
        'schema_version': 1, 'normal_finished': ['run1/task'], 'results': [],
    }))
    scheduler = scheduler_class()(None, 1)
    calls = []
    async def execute(command, log_file, **kwargs):
        d = Path(log_file).parent
        calls.append(d.parent.parent.name)
        result(d)
        return {'success': True, 'returncode': 0}
    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    await module.execute_repetitions(['task'], 1, 'model', tmp_path, Path('config.json'), scheduler)
    assert calls == ['run1']
    assert json.loads((tmp_path / 'repeat_results.json').read_text())['results'][0]['completed']
