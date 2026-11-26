import ast
import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts.model_api_guard import classify, raise_if_fatal, FatalModelAPIError, MARKER, gather_or_cancel
from scripts.n_run import execute_repetitions, has_terminal_result
from scripts.task_retry import retry_marker
from test_n_run_repetitions import ROOT, result, scheduler_class


class ProviderError(Exception):
    def __init__(self, status, body):
        self.status_code = status
        self.body = body


@pytest.mark.parametrize('status,body,reason', [
    (401, {'message': 'Unauthorized'}, 'authentication_failed'),
    (403, {'error': {'code': 'invalid_api_key'}}, 'authentication_failed'),
    (402, {}, 'billing_exhausted'),
    (429, {'error': {'code': 'insufficient_quota'}}, 'billing_exhausted'),
    (400, {'error': {'message': '账户余额不足请充值'}}, 'billing_exhausted'),
    (503, {'error': {'message': 'Service model-123 is unavailable with status: STOPPED'}}, 'service_stopped'),
    (503, {'error': {'status': 'STOPPED', 'message': 'deployment stopped'}}, 'service_stopped'),
    (429, {'error': {'message': 'rate limit exceeded per minute'}}, None),
    (500, {'message': 'Internal server error'}, None),
    (502, {'message': 'Bad gateway'}, None),
    (503, {'message': 'Service unavailable'}, None),
    (504, {'message': 'Upstream Service Timeout'}, None),
    (400, {'message': 'Expecting value: line 1 column 17'}, None),
    (400, {'message': 'maximum context length exceeded'}, None),
    (403, {'message': 'forbidden'}, None),
    (404, {'message': 'model not found'}, None),
    (400, {'request': {'code': 'invalid_api_key'}, 'message': 'bad request'}, None),
    (400, {'messages': [{'content': 'insufficient_quota'}]}, None),
    (400, {'code': 'invalid_token_count'}, None),
])
def test_only_explicit_provider_failure_is_fatal(status, body, reason):
    assert classify(ProviderError(status, body)) == reason


def test_plain_text_is_not_a_provider_error():
    assert classify(Exception('HTTP 401 invalid_api_key')) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('class_name', ['OpenAIChatCompletionsModelWithRetry', 'OpenAIResponsesModelWithRetry'])
@pytest.mark.parametrize('status,expected_calls', [(401, 1), (504, 10)])
async def test_real_retry_method_stops_immediately_only_for_fatal_errors(tmp_path, monkeypatch, class_name, status, expected_calls):
    calls = []

    class Base:
        async def get_response(self, *args, **kwargs):
            calls.append(True)
            raise ProviderError(status, {'message': 'provider failure'})

        raw_get_response = get_response

    # Execute the production retry method without importing unrelated model SDKs.
    tree = ast.parse((ROOT/'utils/api_model/model_provider.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_response')
    node = ast.ClassDef(name='Retry', bases=[ast.Name(id='Base', ctx=ast.Load())], keywords=[], body=[method], decorator_list=[])
    from utils.api_model.interrupted_stream import ModelOutputInterrupted, agent_budget_state
    from time import monotonic
    truncated = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TruncatedStreamError")
    namespace = {'Base': Base, 'asyncio': asyncio, 'raise_if_fatal': raise_if_fatal,
                 'ModelOutputInterrupted': ModelOutputInterrupted, 'agent_budget_state': agent_budget_state,
                 'monotonic': monotonic}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[truncated, node], type_ignores=[])), '<retry>', 'exec'), namespace)
    model = namespace['Retry']()
    model.retry_times, model.retry_delay, model.debug = 10, 0, False
    marker = tmp_path/MARKER
    monkeypatch.setenv('TOOLATHLON_FATAL_API_FILE', str(marker))
    with pytest.raises(FatalModelAPIError if status == 401 else Exception):
        await model.get_response()
    assert len(calls) == expected_calls
    assert marker.exists() is (status == 401)
    if marker.exists():
        assert json.loads(marker.read_text()) == {'reason': 'authentication_failed', 'status_code': 401}


@pytest.mark.asyncio
async def test_breaker_cancels_siblings_preserves_finished_and_resumes(tmp_path, monkeypatch):
    preserved = tmp_path/'run1/finalpool/finished'
    result(preserved, False)
    before = (preserved/'eval_res.json').read_bytes()
    scheduler = scheduler_class()(None, 2)
    sibling_started = asyncio.Event()
    stopped = []

    async def failing(command, log_file, **kwargs):
        directory = Path(log_file).parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory/'status.json').write_text('{"preprocess":"done","running":"running"}')
        if directory.name == 'fatal':
            await sibling_started.wait()
            (directory/MARKER).write_text('{"reason":"service_stopped","status_code":503}')
        else:
            sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(directory.name)

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', failing)
    with pytest.raises(FatalModelAPIError, match='service_stopped'):
        await asyncio.wait_for(execute_repetitions(
            ['finished', 'fatal', 'sibling'], 3, 'm', tmp_path, Path('config.json'), scheduler), 3)
    assert 'fatal' in stopped and 'finished' in stopped
    assert not (tmp_path/'run1/finalpool/sibling').exists()
    assert (preserved/'eval_res.json').read_bytes() == before
    assert not has_terminal_result(tmp_path/'run1/finalpool/fatal')
    assert not retry_marker(tmp_path/'run1/finalpool/fatal').exists()

    resumed = scheduler_class()(None, 2)
    calls = []

    async def recovered(command, log_file, **kwargs):
        directory = Path(log_file).parent
        calls.append(directory)
        result(directory)
        (directory/'run.log').write_text('done')
        return {'success': True, 'returncode': 0}

    monkeypatch.setitem(resumed._execute_task.__globals__, 'run_command_async', recovered)
    await execute_repetitions(['finished', 'fatal', 'sibling'], 3, 'm', tmp_path, Path('config.json'), resumed)
    assert preserved not in calls
    assert len(calls) == 8
    assert has_terminal_result(tmp_path/'run1/finalpool/fatal')


@pytest.mark.asyncio
async def test_fatal_error_during_an_ordinary_retry_remains_resumable(tmp_path, monkeypatch):
    from test_task_api_retry import api_failure
    scheduler = scheduler_class()(None, 1)

    async def execute(command, log_file, **kwargs):
        directory = Path(log_file).parent
        if '_api_retry' not in str(directory):
            api_failure(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            (directory/MARKER).write_text('{"reason":"billing_exhausted","status_code":402}')
        return {'success': False, 'returncode': 1}

    monkeypatch.setitem(scheduler._execute_task.__globals__, 'run_command_async', execute)
    with pytest.raises(FatalModelAPIError):
        await scheduler.run_single_task('finalpool/task', 'full', 'm', 'unified', '200', dump_path=str(tmp_path/'run1'))
    assert retry_marker(tmp_path/'run1/finalpool/task').exists()
    assert not has_terminal_result(tmp_path/'run1/finalpool/task')


@pytest.mark.asyncio
async def test_real_process_is_stopped_before_waiting_for_task_exit(tmp_path, monkeypatch):
    scheduler = scheduler_class()(None, 1)
    namespace = scheduler._execute_task.__globals__
    namespace.update({'signal': signal, 'subprocess': subprocess, 'datetime': datetime, 'os': os})
    names = {'run_command_async', 'cleanup_single_process', '_cleanup_process_group'}
    nodes = [node for node in ast.parse((ROOT/'run_parallel.py').read_text()).body
             if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<real process cleanup>', 'exec'), namespace)
    task_dir = tmp_path/'run1/finalpool/task'
    script = tmp_path/'scripts/run_single_containerized.sh'
    script.parent.mkdir()
    stopped = tmp_path/'child-stopped'
    # Match the native runner command without starting Docker or using a Hub account.
    script.write_text(f'''#!/bin/bash
exec {sys.executable} -u - <<'PY'
import json,signal,time
from pathlib import Path
marker=Path({str(task_dir/MARKER)!r})
marker.parent.mkdir(parents=True,exist_ok=True)
def stop(*args):
    Path({str(stopped)!r}).write_text('cleaned')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
marker.write_text(json.dumps({{'reason':'authentication_failed','status_code':401}}))
time.sleep(60)
PY
''')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FatalModelAPIError):
        await asyncio.wait_for(scheduler.run_single_task(
            'finalpool/task', 'full', 'm', 'unified', '200', dump_path=str(tmp_path/'run1')), 5)
    assert stopped.read_text() == 'cleaned'
    assert not scheduler.active_processes
    assert not namespace['active_processes']
