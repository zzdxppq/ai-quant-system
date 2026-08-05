#!/usr/bin/env bash
# 仅重跑今日选股 + 决策卡 + 邮件（不动周五复盘/涨停池）
#
# 用法:
#   sudo bash scripts/deploy/server_rerun_screener.sh
#   sudo bash scripts/deploy/server_rerun_screener.sh --no-email
#
# 默认 --auction-time（9:27 竞价口径）

set -euo pipefail

QUANT_APP_DIR="${QUANT_APP_DIR:-/opt/ai-quant-system}"
QUANT_CONTAINER="${QUANT_CONTAINER:-quant-ai}"
QUANT_IMAGE="${QUANT_IMAGE:-registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant-service:20260525.1250}"
QUANT_ENV_FILE="${QUANT_ENV_FILE:-$QUANT_APP_DIR/.env}"

EXTRA_ARGS=(--auction-time)
if [[ $# -gt 0 ]]; then
  EXTRA_ARGS=(--auction-time "$@")
fi

log() { echo "[$(date '+%F %T')] $*"; }

if [[ ! -f "$QUANT_ENV_FILE" ]]; then
  echo "FAIL: 未找到 $QUANT_ENV_FILE" >&2
  exit 1
fi

log "停止 $QUANT_CONTAINER …"
docker stop "$QUANT_CONTAINER" 2>/dev/null || true

log "重跑选股 (args: ${EXTRA_ARGS[*]}) …"
docker run --rm \
  --env-file "$QUANT_ENV_FILE" \
  -e TZ=Asia/Shanghai \
  -v "$QUANT_APP_DIR/data:/app/data" \
  -v "$QUANT_APP_DIR/logs:/app/logs" \
  "$QUANT_IMAGE" \
  python -u scripts/rerun_screener_today.py "${EXTRA_ARGS[@]}"

log "启动 $QUANT_CONTAINER …"
docker start "$QUANT_CONTAINER"

log "验证:"
curl -s "http://127.0.0.1:8001/api/screener" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  date', d.get('date'))
print('  hits', len(d.get('hits') or []))
for h in (d.get('hits') or []):
    print('   ', h.get('code'), h.get('name'), h.get('auction_gain'), h.get('continuous_limit_up'))
" || true

log "完成"
