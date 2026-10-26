#!/usr/bin/env bash
#
# list_subfolders.sh
#
# 将「给定文件夹」下的子文件夹名字全部写入到指定地址的 .txt 文件中。
# 每行一个名字，按字母序排序，仅写入一级子文件夹的 basename（不含父路径、结尾斜杠）。
#
# 用法:
#   bash scripts/list_subfolders.sh <源文件夹> <输出txt路径>
#
# 示例:
#   bash scripts/list_subfolders.sh tasks/finalpool task_files/all_tasks.txt
#
# 行为说明:
#   - 只列出一级子文件夹（不递归，不包含文件）。
#   - 隐藏文件夹（以 . 开头）默认也会列出；如需排除可去掉下面的注释或加 -A 改为 -A。
#   - 输出按 LC_ALL=C 字典序排序，行尾使用 Unix \n。
#   - 若输出目录不存在会自动创建。
#   - 若 <输出txt路径> 为 -，则输出到 stdout 而不写文件。
#
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash scripts/list_subfolders.sh <source_dir> <output_txt>
  <source_dir>  要列举子文件夹的目录（例如 tasks/finalpool）
  <output_txt>  输出文件路径（例如 task_files/all_tasks.txt），传 - 表示输出到 stdout
EOF
}

if [[ $# -ne 2 ]]; then
    usage
    exit 1
fi

SOURCE_DIR="$1"
OUTPUT_TXT="$2"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: source directory not found: $SOURCE_DIR" >&2
    exit 1
fi

# 生成排序后的子文件夹名列表（仅一级子目录，basename 形式）
# 使用 find + -maxdepth 1 + -type d 比 glob 更稳健（能匹配以 - 开头的目录名等）
mapfile -t SUBFOLDERS < <(
    find "$SOURCE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | LC_ALL=C sort
)

if [[ "$OUTPUT_TXT" == "-" ]]; then
    # 输出到 stdout
    printf '%s\n' "${SUBFOLDERS[@]}"
else
    # 确保输出目录存在
    OUT_DIR="$(dirname "$OUTPUT_TXT")"
    mkdir -p "$OUT_DIR"
    # 写入文件（每行一个名字）
    printf '%s\n' "${SUBFOLDERS[@]}" > "$OUTPUT_TXT"
    echo "Wrote ${#SUBFOLDERS[@]} subfolder(s) to $OUTPUT_TXT"
fi