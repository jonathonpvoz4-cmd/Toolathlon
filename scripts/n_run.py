#!/usr/bin/env python3
"""Run N task repetitions with one deployment and one shared scheduler.

Different tasks advance through normal repetitions independently. After all
normal attempts finish, retry eligible failures in separate round directories.
Both stages use the same concurrency limit and native task conflict groups.
In Hub mode, invoke through scripts/mcphub/run_with_workspace.py once.
"""

import argparse
import asyncio
import json
import random
from pathlib import Path
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.task_retry import (
    claim_deferred_retry, deferred_retry_reason, has_terminal_result, selected_result,
)

DEFAULT_RESULT_OUTPUT_PATH = "/mnt/djc/dumps_public_service"
DEFAULT_MAX_WORKERS = 32
PROVIDER = "unified"

DEPLOY_SCRIPT = "global_preparation/deploy_containers.sh"
BOOTSTRAP_SCRIPT = "scripts/bootstrap_notion_official.sh"
RUN_SCRIPT = "scripts/run_parallel_for_n_runs.sh"


def _as_root() -> bool:
    """True when the process can already touch docker/ports without sudo."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _command(script, *args, needs_sudo):
    """Build the shell command for a script, adding ``sudo`` when required."""
    cmd = []
    if needs_sudo and not _as_root():
        cmd.append("sudo")
        if os.getenv("TOOLATHLON_HUB_SNAPSHOT") == "1" or script == RUN_SCRIPT:
            # Preserve routing across sudo without placing credential values in argv.
            routing_names = (
                "MCP_TOOL_TRANSPORT", "MCPHUB_URL", "MCPHUB_TOKEN", "MCPHUB_CATALOG",
                "MCPHUB_SERVERS", "MCPHUB_TARGET", "MCPHUB_NAME_SEPARATOR",
                "MCPHUB_CALLBACK_HOST", "MCPHUB_RUN_ID", "MCPHUB_RUN_TOKEN",
                "TOOLATHLON_HUB_SNAPSHOT", "TOOLATHLON_SKIP_GLOBAL_PRUNE", "SERPER_API_URL", "SERPER_API_KEY",
            )
            present = [name for name in routing_names if name in os.environ]
            if present:
                cmd.append("--preserve-env=" + ",".join(present))
    cmd.append("bash")
    cmd.append(script)
    cmd.extend(str(a) for a in args)
    return cmd


def _redact(command_line):
    """Mask secret values before echoing a command to console/logs."""
    return re.sub(r"(--api-key=)\S+", r"\1***", command_line)


def run_step(cmd, log_path):
    """Run ``cmd`` in the repo root, streaming output to stdout and ``log_path``.

    Returns the process exit code.  Output is line-buffered so progress stays
    visible in real time while a durable copy is written to disk for post-mortem.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    header = f"$ {_redact(' '.join(cmd))}\n"
    sys.stdout.write(header)
    sys.stdout.flush()

    with open(log_path, "w") as logf:
        logf.write(header)
        logf.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            logf.write(line)
            logf.flush()
        proc.wait()
        return proc.returncode


def retain_timeout_result(task_dir: Path) -> None:
    """Record an ungraded scheduler timeout, never pretend it was evaluated."""
    if (task_dir / 'eval_res.json').exists() or (task_dir / 'fatal_model_api.json').exists():
        return
    try:
        status = json.loads((task_dir / 'status.json').read_text())
    except (OSError, ValueError):
        return
    if status.get('preprocess') == 'done' and status.get('running') == 'timeout':
        with (task_dir / 'eval_res.json').open('x') as stream:
            json.dump({'pass': None, 'details': 'Execution budget exhausted; no evaluator result available',
                       'outcome': 'ungraded_timeout'}, stream)

def select_tasks(task_list=None):
    root = Path(REPO_ROOT)
    all_tasks = [line.strip() for line in (root / 'task_files/all_tasks_full_108.txt').read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
    if not task_list:
        return all_tasks
    source = Path(task_list)
    if not source.is_absolute():
        source = root / source
    selected = list(dict.fromkeys(line.strip() for line in source.read_text().splitlines()
                                 if line.strip() and not line.lstrip().startswith('#')))
    unknown = set(selected) - set(all_tasks)
    if not selected or unknown:
        raise ValueError('Task list is empty or contains unknown tasks: ' + ', '.join(sorted(unknown)))
    return selected


def task_config_path(config):
    root = Path(REPO_ROOT).resolve()
    source = Path(config)
    if not source.is_absolute():
        source = root / source
    # The repository is copied to /workspace in each task container; the
    # enclosing evaluation's private workspace path is not mounted there.
    return source.resolve().relative_to(root)


async def execute_repetitions(tasks, repetitions, model, output, config, scheduler, max_steps=200):
    from scripts.model_api_guard import gather_or_cancel

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    task_config = task_config_path(config)
    scheduler.defer_task_retries = True
    total = len(tasks) * repetitions
    scheduler.total_tasks = total
    manifest = output / 'repeat_results.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    if manifest.exists() and (not isinstance(previous, dict) or previous.get('schema_version') != 1):
        raise ValueError('Invalid Toolathlon repetition checkpoint')
    finished = set(previous.get('normal_finished', []))
    retry_finished = set()
    pairs = [(index, task) for index in range(1, repetitions + 1) for task in tasks]

    def original(index, task):
        return output / f'run{index}' / 'finalpool' / task

    def complete(directory, source):
        retain_timeout_result(source)
        return has_terminal_result(source, retry_exhausted=source != directory)

    # Existing failed checkpoints are collected rather than repeating their
    # normal attempt before the remaining rounds have run.
    for index, task in pairs:
        directory = original(index, task)
        try:
            status = json.loads((directory / 'status.json').read_text())
        except (OSError, ValueError):
            status = {}
        if (directory / 'fatal_model_api.json').exists():
            finished.discard(f'run{index}/{task}')
        if (not (directory / 'fatal_model_api.json').exists()
                and (status.get('preprocess') in {'fail', 'timeout'}
                     or (status.get('preprocess') == 'done' and status.get('running') in {'fail', 'done', 'max_turn_exceeded'}))):
            finished.add(f'run{index}/{task}')

    def progress():
        rows = []
        counts = dict(passed=0, incorrect=0, unknown=0, execution_failed=0)
        for index, task in pairs:
            directory = original(index, task)
            source = selected_result(directory)
            terminal = complete(directory, source)
            if terminal:
                passed = json.loads((source / 'eval_res.json').read_text()).get('pass')
                counts['passed' if passed is True else 'incorrect' if passed is False else 'unknown'] += 1
            elif f'run{index}/{task}' in retry_finished:
                counts['execution_failed'] += 1
            rows.append({'task': task, 'repetition': index,
                         'directory': source.relative_to(output).as_posix(), 'completed': terminal})
        data = {'schema_version': 1, 'repetitions': repetitions,
                'normal_finished': sorted(finished), 'results': rows}
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False))
        temporary.replace(manifest)
        completed = counts['passed'] + counts['incorrect'] + counts['unknown']
        print(f'[Repetitions Progress] completed={completed}/{total} '
              f'passed={counts["passed"]} incorrect={counts["incorrect"]} '
              f'unknown={counts["unknown"]} execution_failed={counts["execution_failed"]} '
              f'remaining={total - completed - counts["execution_failed"]}', flush=True)

    async def report():
        while True:
            await asyncio.sleep(60)
            progress()

    async def execute(index, task, retry=False):
        directory = original(index, task)
        destination = selected_result(directory) if retry else directory
        print(f'[{"Retry" if retry else "Repeat queued"} {index}/{repetitions}] {task}', flush=True)
        await scheduler.run_single_task(
            'finalpool/' + task, 'full', model, 'unified', str(max_steps),
            timeout=36000, eval_config=str(task_config), dump_path=str(destination.parent.parent),
            runner='containerized',
        )
        if retry:
            retry_finished.add(f'run{index}/{task}')
        else:
            finished.add(f'run{index}/{task}')
        progress()

    progress()
    reporter = asyncio.create_task(report())
    try:
        async def normal_chain(task):
            for index in range(1, repetitions + 1):
                directory = original(index, task)
                if (selected_result(directory) != directory or complete(directory, directory)
                        or f'run{index}/{task}' in finished):
                    continue
                await execute(index, task)
                reason = deferred_retry_reason(directory)
                if reason:
                    print(f'[Retry collected {index}/{repetitions}] {task}: {reason}', flush=True)

        await gather_or_cancel(normal_chain(task) for task in tasks)
        print('[Retry stage] All normal attempts finished; starting collected retries', flush=True)

        # Each task's retries stay sequential. Different tasks share the same
        # limit, after the normal pool has completely drained.
        pending = {task: [] for task in tasks}
        for index, task in pairs:
            directory = original(index, task)
            source = selected_result(directory)
            if source != directory:
                if not complete(directory, source):
                    pending[task].append(index)
                continue
            reason = deferred_retry_reason(directory)
            if reason:
                claim_deferred_retry(directory, reason)
                pending[task].append(index)
        progress()

        async def retry_chain(task):
            for index in pending[task]:
                await execute(index, task, retry=True)

        await gather_or_cancel(retry_chain(task) for task in tasks)
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)


def summarize(output, repetitions, config, task_list=None):
    from scripts.generate_parallel_stats import generate_enhanced_stats
    for index in range(1, repetitions + 1):
        directory = output / f'run{index}'
        directory.mkdir(parents=True, exist_ok=True)
        selected = [selected_result(directory / 'finalpool' / task) for task in select_tasks(task_list)]
        generate_enhanced_stats(str(directory), 'finalpool', str(config),
                                str(task_list or Path(REPO_ROOT) / 'task_files/all_tasks_full_108.txt'),
                                task_result_dirs=selected)
        # Only current task files are included, never archived failed attempts.
        for source, target in [('run.log', 'run_all.log'), ('container.log', 'container_all.log'),
                               ('eval_res.json', 'eval_res_all.jsonl'), ('traj_log.json', 'traj_log_all.jsonl')]:
            with (directory / target).open('w') as stream:
                for path in sorted(task / source for task in selected if (task / source).is_file()):
                    if path.suffix == '.json':
                        stream.write(json.dumps(json.loads(path.read_text()), ensure_ascii=False) + '\n')
                    else:
                        stream.write(path.read_text(errors='replace') + '\n')


async def run_repetitions(model, output, repetitions, workers, config, task_list=None, max_steps=200):
    from run_parallel import AsyncTaskScheduler
    from scripts.platform.task_bridge import job_service_bridge
    root = Path(REPO_ROOT)
    tasks = select_tasks(task_list)
    random.shuffle(tasks)
    groups = json.loads((root / 'tasks/finalpool/task_conflict.json').read_text()).get('conflict_groups', [])
    scheduler = AsyncTaskScheduler(groups, workers)
    with job_service_bridge(root):
        await execute_repetitions(tasks, repetitions, model, output, config, scheduler, max_steps)
    summarize(output, repetitions, config, task_list)
    rounds = []
    for index in range(1, repetitions + 1):
        originals = [output / f'run{index}' / 'finalpool' / task for task in tasks]
        directories = [selected_result(path) for path in originals]
        complete = sum(has_terminal_result(path, retry_exhausted=path != original)
                       for original, path in zip(originals, directories))
        passed = sum(json.loads((path / 'eval_res.json').read_text()).get('pass') is True
                     for path in directories if (path / 'eval_res.json').is_file())
        rounds.append({'repetition': index, 'completed': complete, 'n': len(tasks),
                       'n_passed': passed, 'pass_rate': passed / len(tasks) if complete == len(tasks) else None})
    complete = all(row['completed'] == len(tasks) for row in rounds)
    summary = {'repetitions': repetitions, 'rounds': rounds,
               'mean_pass_rate': sum(row['pass_rate'] for row in rounds) / repetitions if complete else None}
    (output / 'repeat_summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    if not complete:
        raise RuntimeError('Some task repetitions are incomplete; checkpoints retained for resume')


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__
    )
    parser.add_argument("--N", type=int, default=3, help="number of repetitions (default: 3)")
    parser.add_argument("--model_name", required=True, help="model short name to evaluate")
    parser.add_argument(
        "--result_output_path",
        nargs="?",
        default=DEFAULT_RESULT_OUTPUT_PATH,
        help="base directory for results (default: %(default)s)",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        nargs="?",
        default=DEFAULT_MAX_WORKERS,
        help="max concurrency per round (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="override TOOLATHLON_OPENAI_BASE_URL (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="override TOOLATHLON_OPENAI_API_KEY (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="override CONFIG_FILE_ARG / eval config path (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and resolved output dirs without executing anything",
    )
    parser.add_argument('--task-list', '--task_list', default=os.getenv('TASK_LIST') or None,
                        help='Task name list file; defaults to all 108 tasks')
    parser.add_argument('--max-steps', type=int, default=200)
    args = parser.parse_args()

    if not 1 <= args.N <= 10:
        parser.error("N must be 1–10")
    if not args.model_name.strip():
        parser.error("model_name must be non-empty")
    if args.max_workers < 1:
        parser.error("max_workers must be >= 1")
    if args.max_steps < 1:
        parser.error('max-steps must be >= 1')

    result_root = os.path.abspath(args.result_output_path)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.model_name).strip(".") or "model"
    run_dirs = [
        os.path.join(result_root, label, f"run{i}")
        for i in range(1, args.N + 1)
    ]

    print("=" * 80)
    print("Toolathlon N-run plan")
    print(f"  rounds:        {args.N}")
    print(f"  model:         {args.model_name}")
    print(f"  result root:   {result_root}")
    print(f"  max workers:   {args.max_workers}")
    print(f"  provider:      {PROVIDER}")
    print(f"  base url:      {args.base_url or '(run_parallel.sh default)'}")
    print(f"  api key:       {'***' if args.api_key else '(run_parallel.sh default)'}")
    print(f"  config file:   {args.config_file or '(run_parallel.sh default)'}")
    print(f"  repo root:     {REPO_ROOT}")
    print("-" * 80)
    for i, d in enumerate(run_dirs, 1):
        print(f"  run{i}: {d}")
    print("=" * 80)

    if args.dry_run:
        print("Dry run — nothing executed.")
        return 0

    if os.getenv("MCP_TOOL_TRANSPORT", "direct") == "hub":
        if os.getenv("TOOLATHLON_HUB_SNAPSHOT") != "1":
            print("Hub mode requires scripts/mcphub/run_with_workspace.py before deployment.", file=sys.stderr)
            return 1
        required = ["deployment/k8s/scripts/setup.sh", "deployment/canvas/scripts/setup.sh",
                    "deployment/poste/scripts/setup.sh", "deployment/woocommerce/scripts/setup.sh"]
        missing = [path for path in required if not os.path.isfile(os.path.join(REPO_ROOT, path))]
        if missing:
            print("Missing original Toolathlon deployment files: " + ", ".join(missing), file=sys.stderr)
            return 1

    model_dir = Path(result_root) / label
    model_dir.mkdir(parents=True, exist_ok=True)
    if args.base_url is not None:
        os.environ['TOOLATHLON_OPENAI_BASE_URL'] = args.base_url
    if args.api_key is not None:
        os.environ['TOOLATHLON_OPENAI_API_KEY'] = args.api_key
    os.environ['TOOLATHLON_REPETITIONS'] = str(args.N)
    config = Path(args.config_file or os.environ.get('CONFIG_FILE_ARG') or 'scripts/formal_run_v0.json')
    if not config.is_absolute():
        config = Path(REPO_ROOT) / config
    if not config.is_file():
        parser.error('Evaluation config does not exist')
    try:
        task_config_path(config)
    except ValueError:
        parser.error('Evaluation config must be inside the Toolathlon repository')
    task_list = Path(args.task_list) if args.task_list else None
    if task_list and not task_list.is_absolute():
        task_list = Path(REPO_ROOT) / task_list
    select_tasks(task_list)  # Validate before any application reset.
    # Shared applications are deployed once; each task resets its own state.
    for script, needs_sudo, extra, log in [
        (BOOTSTRAP_SCRIPT, False, [], 'bootstrap_notion.log'),
        (DEPLOY_SCRIPT, True, ['true'], 'deploy.log'),
    ]:
        code = run_step(_command(script, *extra, needs_sudo=needs_sudo), str(model_dir / log))
        if code:
            return code
    sys.path.insert(0, REPO_ROOT)
    from run_parallel import main_with_signal_handling
    return asyncio.run(main_with_signal_handling(lambda: run_repetitions(
        args.model_name, model_dir, args.N, args.max_workers, config, task_list, args.max_steps)))


if __name__ == "__main__":
    sys.exit(main())
