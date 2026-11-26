import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize('failed,code,expected', [
    ('bootstrap', 2, ['bootstrap']),
    ('deploy', 3, ['bootstrap', 'deploy']),
    ('evaluate', 7, ['bootstrap', 'deploy', 'evaluate']),
    ('none', 0, ['bootstrap', 'deploy', 'evaluate']),
])
def test_single_run_stops_on_failure_and_keeps_exit_status(tmp_path, monkeypatch, failed, code, expected):
    spec = importlib.util.spec_from_file_location('single_run', Path(__file__).resolve().parents[1] / 'scripts/platform/single_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    for key, value in {'MCP_TOOL_TRANSPORT': 'hub', 'TOOLATHLON_HUB_SNAPSHOT': '1',
                       'TOOLATHLON_OPENAI_BASE_URL': 'http://model.test/v1', 'TOOLATHLON_OPENAI_API_KEY': 'test-key'}.items():
        monkeypatch.setenv(key, value)
    trace = tmp_path / 'calls'
    for path, name in [('scripts/bootstrap_notion_official.sh', 'bootstrap'),
                       ('global_preparation/deploy_containers.sh', 'deploy'),
                       ('scripts/run_parallel_for_n_runs.sh', 'evaluate')]:
        script = tmp_path / path
        script.parent.mkdir(exist_ok=True)
        check = 'test "$CONFIG_FILE_ARG" = "model.json" || exit 88\n' if name == 'evaluate' else ''
        script.write_text(f'{check}echo {name} >> "{trace}"\necho {name}\nexit {code if name == failed else 0}\n')
    config = tmp_path / 'model.json'
    config.write_text('{}')
    result = module.run_single('../provider/model', tmp_path / 'results', 10, config)
    assert result == code
    assert trace.read_text().splitlines() == expected
    assert (tmp_path / 'results/_provider_model/run1/bootstrap_notion.log').is_file()


@pytest.mark.parametrize('state,score,trajectory,terminal', [
    ({'preprocess': 'done', 'running': 'done', 'evaluation': True}, True, None, True),
    ({'preprocess': 'done', 'running': 'done', 'evaluation': False}, False, None, True),
    ({'preprocess': 'done', 'running': 'fail', 'evaluation': None}, None, 'failed', True),
    ({'preprocess': 'done', 'running': 'max_turn_exceeded'}, None, None, True),
    ({'preprocess': 'done', 'running': 'timeout'}, None, None, True),
    ({'preprocess': 'fail', 'running': None}, None, None, False),
    ({'preprocess': 'done', 'running': 'running'}, None, None, False),
    ({'preprocess': 'done', 'running': 'fail'}, None, 'cancelled', False),
    ({'preprocess': 'done', 'running': 'fail'}, None, None, False),
])
def test_resume_preserves_terminal_model_results(tmp_path, state, score, trajectory, terminal):
    import json
    spec = importlib.util.spec_from_file_location('single_run', Path(__file__).resolve().parents[1] / 'scripts/platform/single_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'status.json').write_text(json.dumps(state))
    (tmp_path / 'eval_res.json').write_text(json.dumps({'pass': score}))
    if trajectory:
        (tmp_path / 'traj_log.json').write_text(json.dumps({'status': trajectory, 'messages': [{'role': 'user', 'content': 'task'}]}))
    assert module.has_terminal_result(tmp_path) is terminal
    (tmp_path / 'eval_res.json').write_text('{')
    assert module.has_terminal_result(tmp_path) is False


def test_resume_passes_only_incomplete_tasks_to_runner(tmp_path, monkeypatch):
    import json
    spec = importlib.util.spec_from_file_location('single_run', Path(__file__).resolve().parents[1] / 'scripts/platform/single_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    for key, value in {'MCP_TOOL_TRANSPORT': 'hub', 'TOOLATHLON_HUB_SNAPSHOT': '1',
                       'TOOLATHLON_OPENAI_BASE_URL': 'http://model/v1', 'TOOLATHLON_OPENAI_API_KEY': 'key'}.items():
        monkeypatch.setenv(key, value)
    (tmp_path / 'task_files').mkdir()
    (tmp_path / 'task_files/all_tasks_full_108.txt').write_text('model-failure\nenvironment-failure\n')
    (tmp_path / 'scripts').mkdir()
    config = tmp_path / 'config.json'
    config.write_text('{}')
    result = tmp_path / 'results/model/run1/finalpool/model-failure'
    result.mkdir(parents=True)
    files = {'status.json': {'preprocess': 'done', 'running': 'fail'},
             'eval_res.json': {'pass': None}, 'traj_log.json': {'status': 'failed', 'messages': [{'role': 'user', 'content': 'task'}]}}
    for name, data in files.items():
        (result / name).write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in result.iterdir()}
    calls = []
    def step(args, log, env):
        calls.append(args)
        assert Path(env['TASK_LIST']).read_text() == 'environment-failure\n'
        return 0
    monkeypatch.setattr(module, 'run_step', step)
    assert module.run_single('model', tmp_path / 'results', 32, config) == 0
    assert len(calls) == 3
    assert before == {p.name: p.read_bytes() for p in result.iterdir()}


def test_mcp_setup_failure_with_empty_failed_trace_is_retried(tmp_path):
    import json
    spec = importlib.util.spec_from_file_location('single_run', Path(__file__).resolve().parents[1] / 'scripts/platform/single_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in {
        'status.json': {'preprocess': 'done', 'running': 'fail', 'evaluation': None},
        'eval_res.json': {'pass': None},
        'traj_log.json': {'status': 'failed', 'messages': [],
                          'key_stats': {'interaction_turns': 0, 'agent_llm_requests': 0}},
    }.items():
        (tmp_path / name).write_text(json.dumps(value))
    assert module.has_terminal_result(tmp_path) is False


def test_platform_delegates_repetitions_to_native_n_run(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('single_run', Path(__file__).resolve().parents[1] / 'scripts/platform/single_run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    for key, value in {'MCP_TOOL_TRANSPORT': 'hub', 'TOOLATHLON_HUB_SNAPSHOT': '1',
                       'TOOLATHLON_OPENAI_BASE_URL': 'http://model/v1', 'TOOLATHLON_OPENAI_API_KEY': 'key'}.items():
        monkeypatch.setenv(key, value)
    config = tmp_path / 'config.json'
    config.write_text('{}')
    calls = []
    monkeypatch.setattr(module, 'run_step', lambda *args: calls.append(args) or 0)
    assert module.run_single('model', tmp_path / 'results', 32, config, 3) == 0
    assert len(calls) == 1
    command = calls[0][0]
    assert command[1] == 'scripts/n_run.py'
    assert command[command.index('--N') + 1] == '3'
    assert command[command.index('--max_workers') + 1] == '32'
