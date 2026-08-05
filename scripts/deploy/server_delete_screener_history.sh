#!/usr/bin/env bash
# 服务器一键：停 quant-ai → 删除选股历史记录 → 启动
#
# 用法（Ubuntu 服务器）:
#   sudo bash scripts/deploy/server_delete_screener_history.sh --date 2026-05-30
#   sudo bash scripts/deploy/server_delete_screener_history.sh --date 2026-05-30 --code 600162
#   sudo bash scripts/deploy/server_delete_screener_history.sh --date 2026-05-30 --dry-run
#
# 环境变量（可选）:
#   QUANT_APP_DIR   默认 /opt/ai-quant-system
#   QUANT_CONTAINER 默认 quant-ai
#   QUANT_IMAGE     默认 registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant-service:20260523.0249
#   QUANT_ENV_FILE  默认 $QUANT_APP_DIR/.env

set -euo pipefail

QUANT_APP_DIR="${QUANT_APP_DIR:-/opt/ai-quant-system}"
QUANT_CONTAINER="${QUANT_CONTAINER:-quant-ai}"
QUANT_IMAGE="${QUANT_IMAGE:-registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant-service:20260523.0249}"
QUANT_ENV_FILE="${QUANT_ENV_FILE:-$QUANT_APP_DIR/.env}"

log() { echo "[$(date '+%F %T')] $*"; }

if [[ ! -f "$QUANT_ENV_FILE" ]]; then
  echo "FAIL: 未找到 env 文件: $QUANT_ENV_FILE" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "用法: $0 --date YYYY-MM-DD [--code XXXXXX] [--dry-run]" >&2
  exit 1
fi

log "停止容器 $QUANT_CONTAINER …"
docker stop "$QUANT_CONTAINER" 2>/dev/null || true

log "执行 delete_screener_history.py (image=$QUANT_IMAGE) …"
docker run --rm \
  --env-file "$QUANT_ENV_FILE" \
  -e TZ=Asia/Shanghai \
  -v "$QUANT_APP_DIR/data:/app/data" \
  -v "$QUANT_APP_DIR/logs:/app/logs" \
  "$QUANT_IMAGE" \
  python -u scripts/delete_screener_history.py "$@"

log "启动容器 $QUANT_CONTAINER …"
docker start "$QUANT_CONTAINER"

log "最近日志:"
docker logs "$QUANT_CONTAINER" --tail 20

log "完成。"