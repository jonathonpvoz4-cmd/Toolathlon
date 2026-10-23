#!/usr/bin/env bash
#
# get_traj_log_data.sh
#
# 读取 execution_report 的 full JSON，解析出 failed_tasks 名单，
# 只把这些 failed task 里的 traj_log.json 复制到
# /mnt/djc/temp_output/traj_log_files，再将整个 traj_log_files 文件夹
# 打包压缩，最终把压缩包输出到 /mnt/djc。
#
# 用法:
#   bash scripts/get_traj_log_data.sh <execution_report_full.json>
#
# 示例:
#   bash scripts/get_traj_log_data.sh \
#       /mnt/djc/dumps_public_service/92B-v21-B002-stage2-15406-agent1/run1/execution_report_finalpool_92B-v21-B002-stage2-15406-agent1_full.json
#
# 行为说明:
#   - 从 JSON 的 details.failed_tasks 读取失败任务名单。
#   - 每个 failed task 里的 traj_log.json 会复制为 <task_name>_traj_log.json，
#     避免不同 task 之间同名文件互相覆盖。
#   - 复制目录 /mnt/djc/temp_output/traj_log_files 每次运行前会被清空重建，
#     避免残留上一次运行的结果。
#   - 归档名由 report 路径自动推导：<agent>_<run>_failed_traj_logs.tar.gz。
#
set -euo pipefail

OUT_ROOT="/mnt/djc"
TRAJ_DIR="${OUT_ROOT}/temp_output/traj_log_files"

usage() {
    cat <<'EOF'
Usage: bash scripts/get_traj_log_data.sh <execution_report_full.json>
  <execution_report_full.json>  执行报告的 full JSON，例如 .../<agent>/<run>/execution_report_*_full.json
EOF
}

if [[ $# -ne 1 ]]; then
    usage
    exit 1
fi

REPORT="${1%/}"   # 去掉结尾斜杠

if [[ ! -f "$REPORT" ]]; then
    echo "Error: execution report not found: $REPORT" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required but not found" >&2
    exit 1
fi

# 解析 failed_tasks 名单（形如 "finalpool/language-school"）
mapfile -t failed_tasks < <(jq -r '.details.failed_tasks[]?' "$REPORT")

if [[ ${#failed_tasks[@]} -eq 0 ]]; then
    echo "Error: no failed_tasks found in $REPORT" >&2
    exit 1
fi

# 由 report 路径推导 run 目录与 finalpool 目录
RUN_DIR="$(dirname "$REPORT")"
TASKS_FOLDER="$(jq -r '.configuration.tasks_folder // "finalpool"' "$REPORT")"
FINALPOOL_DIR="${RUN_DIR}/${TASKS_FOLDER}"

if [[ ! -d "$FINALPOOL_DIR" ]]; then
    echo "Error: finalpool directory not found: $FINALPOOL_DIR" >&2
    exit 1
fi

# 清空并重建临时输出目录，避免残留上一次的结果
rm -rf "$TRAJ_DIR"
mkdir -p "$TRAJ_DIR"

# 归档名：.../<agent>/<run>/execution_report_..._full.json -> <agent>_<run>_failed_traj_logs.tar.gz
RUN_NAME="$(basename "$RUN_DIR")"
AGENT_NAME="$(basename "$(dirname "$RUN_DIR")")"
ARCHIVE="${OUT_ROOT}/${AGENT_NAME}_${RUN_NAME}_failed_traj_logs.tar.gz"

copied=0
missing=0

# 只处理 failed_tasks 名单里的 task
for task in "${failed_tasks[@]}"; do
    task_name="$(basename "$task")"
    traj="${FINALPOOL_DIR}/${task_name}/traj_log.json"
    if [[ -f "$traj" ]]; then
        cp "$traj" "${TRAJ_DIR}/${task_name}_traj_log.json"
        copied=$((copied + 1))
    else
        echo "Warning: no traj_log.json in ${task_name}, skipped" >&2
        missing=$((missing + 1))
    fi
done

if [[ $copied -eq 0 ]]; then
    echo "Error: no failed-task traj_log.json found under $FINALPOOL_DIR" >&2
    exit 1
fi

# 打包压缩 traj_log_files 目录
tar -czf "$ARCHIVE" -C "${OUT_ROOT}/temp_output" traj_log_files

echo "Copied ${copied} traj_log.json file(s) (${missing} missing) to ${TRAJ_DIR}"
echo "Archive written to ${ARCHIVE}"
