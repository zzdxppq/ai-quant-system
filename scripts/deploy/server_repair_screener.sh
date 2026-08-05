#!/usr/bin/env bash
# 服务器一键：停 quant-ai → 修正上一交易日涨停池 → 同步复盘 → 重跑选股+邮件 → 启动
#
# 用法（Ubuntu 服务器）:
#   sudo bash scripts/deploy/server_repair_screener.sh
#   sudo bash scripts/deploy/server_repair_screener.sh --no-email
#   sudo bash scripts/deploy/server_repair_screener.sh --limit-up-date 20260522
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

# 默认清理 limit_up_cache 中的周六/日伪键
DEFAULT_EXTRA=(--purge-non-trading-keys)

log() { echo "[$(date '+%F %T')] $*"; }

if [[ ! -f "$QUANT_ENV_FILE" ]]; then
  echo "FAIL: 未找到 env 文件: $QUANT_ENV_FILE" >&2
  exit 1
fi

EXTRA_ARGS=("${DEFAULT_EXTRA[@]}")
EXTRA_ARGS+=("$@")

log "停止容器 $QUANT_CONTAINER …"
docker stop "$QUANT_CONTAINER" 2>/dev/null || true

log "执行 repair_prev_day_screener.py (image=$QUANT_IMAGE) …"
docker run --rm \
  --env-file "$QUANT_ENV_FILE" \
  -e TZ=Asia/Shanghai \
  -v "$QUANT_APP_DIR/data:/app/data" \
  -v "$QUANT_APP_DIR/logs:/app/logs" \
  "$QUANT_IMAGE" \
  python -u scripts/repair_prev_day_screener.py "${EXTRA_ARGS[@]}"

log "启动容器 $QUANT_CONTAINER …"
docker start "$QUANT_CONTAINER"

log "最近日志:"
docker logs "$QUANT_CONTAINER" --tail 30

log "完成。验证: curl -s http://127.0.0.1:8001/api/screener | head"
