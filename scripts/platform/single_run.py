"""One evaluation: verify authorization, prepare applications, execute tasks."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.n_run import has_terminal_result, retain_timeout_result, select_tasks
from scripts.mcphub.run_with_workspace import terminate
from scripts.platform.task_bridge import job_service_bridge


def run_step(args: list[str], log: Path, env: dict[str, str]) -> int:
    print(f'Running {args[1]}', flush=True)
    with log.open('w') as output:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
        stopping = None
        def request_stop(signum, _frame):
            nonlocal stopping
            if stopping is None:
                stopping = signum
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
        previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for line in process.stdout:
                print(line, end='', flush=True)
                output.write(line)
                output.flush()
            code = process.wait()
            return 128 + stopping if stopping is not None else code
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if process.poll() is None:
                terminate(process)


def run_single(model: str, output: Path, workers: int, config: Path, repetitions: int = 1, task_list: Path | None = None, max_steps: int = 200) -> int:
    if os.getenv('MCP_TOOL_TRANSPORT') != 'hub' or os.getenv('TOOLATHLON_HUB_SNAPSHOT') != '1':
        raise ValueError('Platform evaluation requires an acquired Hub workspace snapshot')
    if not os.getenv('TOOLATHLON_OPENAI_BASE_URL') or not os.getenv('TOOLATHLON_OPENAI_API_KEY'):
        raise ValueError('Platform model connection is not configured')
    if workers < 1 or max_steps < 1 or not 1 <= repetitions <= 10 or not model.strip() or not config.is_file():
        raise ValueError('Invalid model, concurrency or config file')
    label = re.sub(r'[^A-Za-z0-9_.-]+', '_', model).strip('.') or 'model'
    run_dir = output.resolve() / label / 'run1'
    run_dir.mkdir(parents=True, exist_ok=True)
    # Task containers receive this file under /workspace/scripts, rather than
    # the parent's private snapshot path.
    relative_config = config.resolve().relative_to(ROOT.resolve())
    env = {**os.environ, 'CONFIG_FILE_ARG': relative_config.as_posix(), 'MAX_STEPS': str(max_steps)}
    task_file = task_list or ROOT / 'task_files/all_tasks_full_108.txt'
    if repetitions > 1:
        command = [
            sys.executable, 'scripts/n_run.py', '--N', str(repetitions), '--model_name', model,
            '--result_output_path', str(output.resolve()), '--max_workers', str(workers),
            '--config-file', str(relative_config),
            '--max-steps', str(max_steps),
        ]
        if task_list:
            command += ['--task-list', str(task_list)]
        return run_step(command, run_dir.parent / 'run_parallel.log', env)
    if task_file.is_file():
        tasks = [line.strip() for line in task_file.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        for task in tasks:
            retain_timeout_result(run_dir / 'finalpool' / task)
        pending = [task for task in tasks if not has_terminal_result(run_dir / 'finalpool' / task)]
        print(f'Checkpoint: preserving {len(tasks) - len(pending)}/{len(tasks)} terminal results; '
              f'executing {len(pending)} tasks.', flush=True)
        if not pending:
            return 0
        pending_file = ROOT / 'scripts/platform-pending-tasks.txt'
        pending_file.write_text('\n'.join(pending) + '\n')
        env['TASK_LIST'] = str(pending_file)
    steps = [
        (['bash', 'scripts/bootstrap_notion_official.sh'], 'bootstrap_notion.log'),
        (['bash', 'global_preparation/deploy_containers.sh', 'true'], 'deploy.log'),
        (['bash', 'scripts/run_parallel_for_n_runs.sh', model, str(run_dir), 'unified', str(workers)], 'run_parallel.log'),
    ]
    for command, log in steps:
        if log == 'run_parallel.log':
            with job_service_bridge(ROOT):
                code = run_step(command, run_dir / log, env)
        else:
            code = run_step(command, run_dir / log, env)
        if code:
            print(f'{command[1]} failed (exit {code}); see {run_dir / log}', file=sys.stderr, flush=True)
            return code
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--task-list', type=Path)
    parser.add_argument('--max-steps', type=int, default=200)
    args = parser.parse_args()
    return run_single(args.model, args.output, args.workers, args.config, args.repetitions, args.task_list, args.max_steps)


if __name__ == '__main__':
    raise SystemExit(main())
