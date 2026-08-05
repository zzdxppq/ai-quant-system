#!/bin/sh
set -e
cd /app
export PYTHONPATH=/app

# 持久化卷挂载 data/ 时，首次启动初始化 DuckDB
if [ ! -f /app/data/quant.duckdb ]; then
  echo "[docker] 初始化 data/quant.duckdb ..."
  python -c "from src.data.models import init_db; init_db(); print('init_db ok')"
fi

exec "$@"
