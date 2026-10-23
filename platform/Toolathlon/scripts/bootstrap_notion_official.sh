#!/usr/bin/env bash
#
# bootstrap_notion_official.sh
#
# 冷启动引导：部署容器、评测容器起来之前，把 Notion 官方 MCP 的 token 刷好。
#
# 背景：
#   - `notion_official` 走 `npx mcp-remote https://mcp.notion.com/mcp`，正常续期由
#     mcp-remote 自己负责；只有 refresh_token 被吊销时才需要走完整 OAuth 往返。
#   - 但有个「假成功」陷阱：access token 未过期而 refresh token 已失效时，mcp-remote
#     不会 401、也不会打印授权 URL，直接 "Connected successfully!"，导致自动机误判成功，
#     下个真正要刷新 token 的容器才会炸。
#   - `--force-round-trip` 先删掉 `<hash>_tokens.json` + PKCE verifier（保留 client_info 复用
#     client_id），再跑自动机，并强制返回值必须为 `reauthorized`，从根上避开假成功。
#
# 用法:
#   bash scripts/bootstrap_notion_official.sh            # 强刷一次（幂等、确定性）
#   bash scripts/bootstrap_notion_official.sh --dry-run  # 只检查前置条件，不触发 OAuth
#
# 前置条件（本脚本会检查）:
#   - configs/.mcp-auth/notion.env 存在（凭据，不进 git，需单独注入）
#   - configs/.mcp-auth/<内部域名指向>/mcp_servers/notion_official.yaml 已配置
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

AUTOAUTH="utils/app_specific/notion/notion_oauth_autoauth.py"
ENV_FILE="configs/.mcp-auth/notion.env"

usage() {
    cat <<'EOF'
Usage: bash scripts/bootstrap_notion_official.sh [--dry-run]
  强刷 Notion 官方 MCP 的 token（完整 OAuth 往返），供部署/评测容器起来前调用。
  可选 --dry-run：只检查前置条件，不触发 OAuth，也不写任何文件。
EOF
}

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
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

# --- 强刷 -------------------------------------------------------------------
printf '正在强刷 Notion token（--force-round-trip）...\n'
uv run python "$AUTOAUTH" --force-round-trip

echo "bootstrap_notion_official=done: token 已就绪"
printf '部署/评测容器可通过 bind-mount configs/.mcp-auth 直接复用。\n'