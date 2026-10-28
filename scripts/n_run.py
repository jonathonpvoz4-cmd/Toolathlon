#!/usr/bin/env python3
"""n_run.py — run the Toolathlon evaluation N times, end-to-end.

Every round reproduces the manual three-step flow, in order:

    1. bash scripts/bootstrap_notion_official.sh                 (verify/save authorization)
    2. sudo bash global_preparation/deploy_containers.sh true   (deploy/reset infra)
    3. sudo bash scripts/run_parallel.sh <model> <run_dir> unified <workers>

Round i's per-task results, aggregated stats and step logs all land under:

    <result_output_path>/<model_name>/run<i>/

For each round the step log files are ``deploy.log``, ``bootstrap_notion.log``
and ``run_parallel.log`` (the last one mirrors the evaluation's live progress,
since run_parallel.sh additionally tees run_parallel.py output into
``<run_dir>/stdout.log``).

Three extra values are forwarded to run_parallel.sh as named arguments:
``--base-url`` -> TOOLATHLON_OPENAI_BASE_URL, ``--api-key`` ->
TOOLATHLON_OPENAI_API_KEY, ``--config-file`` -> CONFIG_FILE_ARG.  Omit any of
them to keep run_parallel.sh's built-in defaults.

Usage:
    python3 scripts/n_run.py --N N --model_name model_name --result_output_path PATH --max_workers N \
        [--base-url URL] [--api-key KEY] [--config-file PATH]
    python3 scripts/n_run.py --dry-run --N N --model_name model_name --result_output_path PATH --max_workers N

Notes:
    - ``sudo`` is used for deploy + eval (matching the manual flow).  It must
      either be passwordless or this process needs a TTY so sudo can prompt.
    - A deploy or Notion-bootstrap failure aborts the entire run (later rounds
      depend on it); an evaluation exit code is recorded but does not stop
      subsequent rounds, since per-task results are already on disk either way.
"""

import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_RESULT_OUTPUT_PATH = "/mnt/djc/dumps_public_service"
DEFAULT_MAX_WORKERS = 10
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Toolathlon evaluation N times (deploy + Notion bootstrap + parallel eval per round)."
    )
    parser.add_argument("--N", type=int, help="number of evaluation rounds to run")
    parser.add_argument("--model_name", help="model short name to evaluate")
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
        default='http://115.120.87.159:8082/v1',
        help="override TOOLATHLON_OPENAI_BASE_URL (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--api-key",
        default='',
        help="override TOOLATHLON_OPENAI_API_KEY (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--config-file",
        default='scripts/temp_configs/deepseek_v4_flash_thinking.json',
        help="override CONFIG_FILE_ARG / eval config path (default: run_parallel.sh built-in)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and resolved output dirs without executing anything",
    )
    args = parser.parse_args()

    if args.N < 1:
        parser.error("N must be >= 1")
    if not args.model_name.strip():
        parser.error("model_name must be non-empty")
    if args.max_workers < 1:
        parser.error("max_workers must be >= 1")

    result_root = os.path.abspath(args.result_output_path)
    run_dirs = [
        os.path.join(result_root, args.model_name, f"run{i}")
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

    eval_exit_codes = {}
    for i, run_dir in enumerate(run_dirs, 1):
        os.makedirs(run_dir, exist_ok=True)
        print(f"\n{'=' * 80}")
        print(f"ROUND {i}/{args.N}")
        print(f"  output dir: {run_dir}")
        print(f"{'=' * 80}")

        # Step 1: verify authorization before any application reset.
        bootstrap_log = os.path.join(run_dir, "bootstrap_notion.log")
        print(f"\n--- Round {i} step 1/3: verify Notion authorization ---")
        rc = run_step(_command(BOOTSTRAP_SCRIPT, needs_sudo=False), bootstrap_log)
        if rc != 0:
            print(
                f"[n_run] Notion bootstrap failed (exit {rc}); aborting. "
                f"See {bootstrap_log}",
                file=sys.stderr,
            )
            return rc

        # Step 2: deploy/reset application infrastructure after authorization is ready.
        deploy_log = os.path.join(run_dir, "deploy.log")
        print(f"\n--- Round {i} step 2/3: deploy containers (true) ---")
        rc = run_step(
            _command(DEPLOY_SCRIPT, "true", needs_sudo=True), deploy_log
        )
        if rc != 0:
            print(
                f"[n_run] deploy failed (exit {rc}); aborting. See {deploy_log}",
                file=sys.stderr,
            )
            return rc

        # Step 3: run the evaluation.
        eval_log = os.path.join(run_dir, "run_parallel.log")
        print(
            f"\n--- Round {i} step 3/3: run evaluation "
            f"(model={args.model_name}, workers={args.max_workers}) ---"
        )
        run_args = [args.model_name, run_dir, PROVIDER, args.max_workers]
        if args.base_url is not None:
            run_args.append(f"--base-url={args.base_url}")
        if args.api_key is not None:
            run_args.append(f"--api-key={args.api_key}")
        if args.config_file is not None:
            run_args.append(f"--config-file={args.config_file}")
        rc = run_step(
            _command(RUN_SCRIPT, *run_args, needs_sudo=True),
            eval_log,
        )
        eval_exit_codes[i] = rc
        if rc != 0:
            # run_parallel.sh can return non-zero on post-processing failure;
            # per-task results are already on disk, so record and continue.
            print(
                f"[n_run] evaluation command exited {rc} (results may still be "
                f"present). See {eval_log}",
                file=sys.stderr,
            )
        print(f"ROUND {i}/{args.N} DONE")

    print("\n" + "=" * 80)
    print("All rounds complete.")
    for i, d in enumerate(run_dirs, 1):
        print(f"  run{i}: {d}   (eval exit={eval_exit_codes[i]})")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())