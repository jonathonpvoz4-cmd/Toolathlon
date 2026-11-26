"""Failed installations must not be accepted as usable infrastructure."""
import ast
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def plugin_step():
    source = (ROOT / 'deployment/woocommerce/scripts/setup.sh').read_text()
    return source.split('# 8. Install WooCommerce\n', 1)[1].split('# 8.5 ', 1)[0]


@pytest.mark.parametrize('failures,exit_code,attempts', [(2, 0, 3), (3, 1, 3)])
def test_plugin_download_retries_bounded_and_failure_stops_setup(tmp_path, failures, exit_code, attempts):
    script = f'''
podman_or_docker=runtime
WOO_WP=fixture
calls=0
runtime() {{
    if [[ "$*" == *"plugin install"* ]]; then
        calls=$((calls + 1))
        echo "$calls" > '{tmp_path / "attempts"}'
        if [ "$calls" -le {failures} ]; then echo 'Bad Gateway' >&2; return 1; fi
    fi
    return 0
}}
sleep() {{ :; }}
{plugin_step()}
echo done > '{tmp_path / "continued"}'
'''
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode == exit_code
    assert int((tmp_path / 'attempts').read_text()) == attempts
    assert (tmp_path / 'continued').exists() == (exit_code == 0)


def test_network_activation_failure_stops_before_site_creation(tmp_path):
    source = (ROOT / 'deployment/woocommerce/scripts/setup.sh').read_text()
    step = source.split('# 15. Network activate WooCommerce plugin\n', 1)[1].split('# 16.', 1)[0]
    result = subprocess.run(['bash', '-c', f'''
podman_or_docker=false
WOO_WP=fixture
{step}
echo done > '{tmp_path / "continued"}'
'''], capture_output=True, text=True)
    assert result.returncode == 1
    assert not (tmp_path / 'continued').exists()


def test_incomplete_store_creation_is_failure(tmp_path):
    source = (ROOT / 'deployment/woocommerce/scripts/setup.sh').read_text()
    step = source.split('# Keep the user list until', 1)[1].split('# End JSON array', 1)[0]
    # The explanatory comment continues on the following fragment's first line.
    step = '\n'.join(step.splitlines()[1:])
    result = subprocess.run(['bash', '-c', f'''
CREATED_COUNT=0
USER_COUNT=20
TEMP_USERS='{tmp_path / "users"}'
touch "$TEMP_USERS"
{step}
echo done > '{tmp_path / "continued"}'
'''], capture_output=True, text=True)
    assert result.returncode == 1
    assert not (tmp_path / 'continued').exists()


def test_artifact_inspection_uses_system_python_even_with_broken_project_environment(tmp_path, monkeypatch):
    from scripts.containerized.task_artifact_guard import ContainerRuntimeClient
    directory = tmp_path / 'task'
    (directory / 'evaluation').mkdir(parents=True)
    (directory / 'groundtruth_workspace').mkdir()
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path / 'broken-venv'))
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert command[:5] == ['docker', 'exec', 'fixture', '/usr/bin/python3', '-I']
        return subprocess.run(command[3:], **kwargs)
    client = ContainerRuntimeClient('docker', 'fixture', command_runner=run)
    snapshot = client.inspect_task(str(directory))
    assert snapshot.root_kind == 'directory'
    assert {e.name for e in snapshot.entries} == {'evaluation', 'groundtruth_workspace'}
    assert len(commands) == 1


def test_single_run_filter_requires_grade_before_skipping_max_turns(tmp_path):
    source = ast.parse((ROOT / 'run_parallel.py').read_text())
    node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'filter_tasks_with_existing_results')
    namespace = dict(os=os, json=json, Path=Path, List=list)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<native result filter>', 'exec'), namespace)
    directory = tmp_path / 'finalpool/task'
    directory.mkdir(parents=True)
    (directory / 'status.json').write_text(json.dumps({'preprocess': 'done', 'running': 'max_turn_exceeded', 'evaluation': None}))
    filtering = namespace[node.name]
    assert filtering(['finalpool/task'], str(tmp_path)) == (['finalpool/task'], [])
    (directory / 'eval_res.json').write_text('{"pass":null}')
    assert filtering(['finalpool/task'], str(tmp_path)) == ([], ['finalpool/task'])
    (directory / 'status.json').unlink()
    (directory / 'run.log').write_text('raise MaxTurnsExceeded("budget exhausted")')
    assert filtering(['finalpool/task'], str(tmp_path)) == ([], ['finalpool/task'])
    (directory / 'eval_res.json').unlink()
    assert filtering(['finalpool/task'], str(tmp_path)) == (['finalpool/task'], [])
