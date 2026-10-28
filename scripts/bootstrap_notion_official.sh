#!/usr/bin/env bash
#
# bootstrap_notion_official.sh
#
# 冷启动引导：部署容器、评测容器起来之前，把 Notion 官方 MCP 的 token 刷好。
#
# Startup checks refresh capability before resetting application data. Missing or
# explicitly rejected grants use the original browser authorization flow. Network
# failures retain the grant and abort; they never trigger destructive token resets.
#
# Usage:
#   bash scripts/bootstrap_notion_official.sh
#   bash scripts/bootstrap_notion_official.sh --dry-run
#   bash scripts/bootstrap_notion_official.sh --force-round-trip  # explicit recovery
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

AUTOAUTH="utils/app_specific/notion/notion_oauth_autoauth.py"
ENV_FILE="configs/.mcp-auth/notion.env"

usage() {
    cat <<'EOF'
Usage: bash scripts/bootstrap_notion_official.sh [--dry-run] [--force-round-trip]
  默认验证刷新能力并连接 MCP；仅缺少或明确失效的授权才重新登录。
  --force-round-trip：显式要求重新授权。
  可选 --dry-run：只检查前置条件，不触发 OAuth，也不写任何文件。
EOF
}

DRY_RUN=0
FORCE_ROUND_TRIP=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force-round-trip) FORCE_ROUND_TRIP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

fail() { echo "bootstrap_notion_official=failed: $1" >&2; exit 1; }

# --- 前置检查 ---------------------------------------------------------------
[ -f "$AUTOAUTH" ] || fail "找不到自动机 $AUTOAUTH"
[ -f "$ENV_FILE" ] || fail "找不到凭据 $ENV_FILE（不进 git，需单独 scp/注入）"

if [ "$DRY_RUN" = 1 ]; then
    echo "bootstrap_notion_official=ok (dry-run: 前置检查通过，未触发 OAuth)"
    exit 0
fi

# Default preflight verifies that refresh credentials work, then probes the MCP.
# A forced interactive round trip remains available for explicit recovery only.
auth_args=(--verify-refresh)
if [ "$FORCE_ROUND_TRIP" = 1 ]; then auth_args=(--force-round-trip); fi
set +e
uv run python "$AUTOAUTH" "${auth_args[@]}"
auth_status=$?
set -e
# Preserve any rotated credentials before deployment, even if the MCP probe failed.
if [ "${TOOLATHLON_HUB_SNAPSHOT:-0}" = 1 ]; then
    uv run python -m utils.mcp.workspace_checkpoint
fi
if [ "$auth_status" != 0 ]; then exit "$auth_status"; fi
echo "bootstrap_notion_official=done: authorization checked and saved"
