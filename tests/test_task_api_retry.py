import asyncio
import json
from pathlib import Path

import pytest

from test_n_run_repetitions import scheduler_class, result
from scripts.n_run import has_terminal_result
from scripts.task_retry import can_retry_api_failure, retry_marker
from scripts.n_run import execute_repetitions


@pytest.mark.asyncio
@pytest.mark.parametrize('succeeds', [True, False])
@pytest.mark.parametrize('marker', ['HUGGINGFACE_MCP_TRANSPORT_FAILURE', 'HUB_TASK_TRANSPORT_FAILURE'])
async def test_lost_tool_sessions_use_one_deferred_environment_retry(tmp_path, monkeypatch, succeeds, marker):
    from scripts.task_retry import deferred_retry_reason, selected_result
    scheduler = scheduler_class()(None, 2)
    calls = []

    async def execute(task, tag, model, provider, maxstep, *args, **kwargs):
        directory = Path(kwargs['dump_path']) / task
        calls.append(directory)
        if '_retry' in str(directory) and succeeds:
            result(directory)
        else:
            result(directory, None, 'fail', [{'role': 'user', 'content': 'task'}])
            (directory/'run.log').write_text(marker + '\n')

    monkeypatch.setattr(scheduler, '_execute_task', execute)
    await execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler)
    assert len(calls) == 6
    assert [p.parent.parent.name for p in calls[:3]] == ['run1', 'run2', 'run3']
    for index in range(1, 4):
        original = tmp_path/f'run{index}/finalpool/task'
        retry = selected_result(original)
        assert retry == tmp_path/f'run{index}_retry/finalpool/task'
        assert deferred_retry_reason(original) is None
        assert has_terminal_result(retry, retry_exhausted=True) is succeeds
        assert not has_terminal_result(original)


@pytest.mark.parametrize('state,passed', [('done', False), ('done', True), ('max_turn_exceeded', None)])
def test_tool_transport_warning_does_not_replace_a_terminal_model_result(tmp_path, state, passed):
    from scripts.task_retry import deferred_retry_reason
    result(tmp_path, passed, state)
    (tmp_path/'run.log').write_text('HUGGINGFACE_MCP_TRANSPORT_FAILURE\n')
    assert deferred_retry_reason(tmp_path) is None


def api_failure(directory, error=504):
    result(directory, None, 'fail', [{'role': 'user', 'content': 'task'}])
    (directory/'run.log').write_text(
        f'Error when running agent - Failed to get response after 10 retries, error: Error code: {error}\n')


@pytest.mark.asyncio
@pytest.mark.parametrize('succeeds', [True, False])
async def test_three_repetitions_make_at_most_six_executions(tmp_path, monkeypatch, succeeds):
    scheduler = scheduler_class()(None, 2)
    scheduler.total_tasks = 3
    calls = []

    async def execute(command, log_file, **kwargs):
        directory = Path(log_file).parent
        calls.append(directory)
        if '_api_retry' in str(directory) and succeeds:
            result(directory)
            (directory/'run.log').write_text('success')
        else:
            api_failure(directory)
        return {'success': succeeds and '_api_retry' in str(directory), 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    for index in range(1, 4):
        await scheduler.run_single_task('finalpool/task', 'full', 'm', 'unified', '200',
                                        dump_path=str(tmp_path/f'run{index}'))
    assert len(calls) == 6
    assert scheduler.completed_tasks == 3
    assert scheduler.failed_tasks == 0
    for index in range(1, 4):
        directory = tmp_path/f'run{index}/finalpool/task'
        assert has_terminal_result(directory)
        assert retry_marker(directory).is_file()
        assert json.loads((directory/'eval_res.json').read_text())['pass'] is (True if succeeds else None)
        assert (directory/'legacy_results/run1/run.log').is_file()
        assert (tmp_path/f'run{index}_api_retry/finalpool/task/run.log').is_file()
    assert scheduler.correct_tasks == (3 if succeeds else 0)


@pytest.mark.asyncio
async def test_resume_uses_only_one_remaining_retry(tmp_path, monkeypatch):
    directory = tmp_path/'run1/finalpool/task'
    api_failure(directory, 400)
    assert not has_terminal_result(directory)
    scheduler = scheduler_class()(None, 1)
    calls = []

    async def execute(command, log_file, **kwargs):
        calls.append(log_file)
        api_failure(Path(log_file).parent, 400)
        return {'success': False, 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    await scheduler.run_single_task('finalpool/task', 'full', 'm', 'unified', '200', dump_path=str(tmp_path/'run1'))
    assert len(calls) == 1
    assert 'run1_api_retry' in calls[0]
    assert has_terminal_result(directory)
    assert not can_retry_api_failure(directory)


@pytest.mark.parametrize('state,passed', [('done', False), ('timeout', None), ('max_turn_exceeded', None)])
def test_normal_model_outcomes_are_not_retried(tmp_path, state, passed):
    result(tmp_path, passed, state)
    (tmp_path/'run.log').write_text('Error when running agent - Failed to get response after 10 retries, error: old warning')
    assert not can_retry_api_failure(tmp_path)


@pytest.mark.asyncio
async def test_custom_step_limit_reaches_each_repetition(tmp_path, monkeypatch):
    scheduler = scheduler_class()(None, 1)
    steps = []

    async def execute(task, tag, model, provider, maxstep, *args, **kwargs):
        steps.append(maxstep)
        result(Path(kwargs['dump_path'])/task)

    monkeypatch.setattr(scheduler, '_execute_task', execute)
    await execute_repetitions(['task'], 3, 'model', tmp_path, Path('config.json'), scheduler, max_steps=250)
    assert steps == ['250', '250', '250']
