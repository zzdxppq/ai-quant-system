#!/usr/bin/env bash
# 在 Ubuntu 服务器上执行（git clone / git pull 到 APP_DIR 之后）:
#   cd /opt/ai-quant-system && sudo bash scripts/deploy/ubuntu_install.sh
#
# 可选环境变量:
#   APP_DIR=/opt/ai-quant-system   安装目录
#   APP_USER=quant                 运行用户
#   API_PORT=8001                  服务端口
#   PYTHON_BIN=python3.12          解释器（需已安装）

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ai-quant-system}"
APP_USER="${APP_USER:-quant}"
API_PORT="${API_PORT:-8001}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"

echo "==> ai-quant-system Ubuntu 安装"
echo "    APP_DIR=$APP_DIR  PORT=$API_PORT  PYTHON=$PYTHON_BIN"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行: sudo bash $0"
  exit 1
fi

if [[ ! -f "$APP_DIR/main.py" ]]; then
  echo "错误: 未找到 $APP_DIR/main.py ，请先把代码同步到 APP_DIR"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ca-certificates curl git \
  build-essential pkg-config \
  libffi-dev libssl-dev \
  fonts-dejavu-core \
  tzdata

# 时区（A 股 cron 按北京时间）
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
dpkg-reconfigure -f noninteractive tzdata 2>/dev/null || true

# Python 3.12（Ubuntu 22.04/24.04 常用 deadsnakes）
if ! command -v "$PYTHON_BIN" &>/dev/null; then
  echo "==> 安装 Python 3.12 (deadsnakes)..."
  apt-get install -y -qq software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -qq
  apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
  PYTHON_BIN=python3.12
fi

"$PYTHON_BIN" --version

# 运行用户
if ! id "$APP_USER" &>/dev/null; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 虚拟环境与依赖（DuckDB 由 pip 安装 duckdb + duckdb-engine，无需 apt 装 duckdb）
echo "==> 创建 venv 并安装 requirements..."
sudo -u "$APP_USER" bash -c "
  set -euo pipefail
  cd '$APP_DIR'
  '$PYTHON_BIN' -m venv .venv
  source .venv/bin/activate
  pip install -U pip wheel setuptools
  pip install -r requirements.txt
  python -c 'import duckdb, uvicorn, fastapi; print(\"deps ok\", duckdb.__version__)'
"

# .env（不覆盖已有）
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_DIR/scripts/deploy/ubuntu.env.example" "$ENV_FILE"
  chown "$APP_USER:$APP_USER" "$ENV_FILE"
fi
# 确保端口为 8001
if grep -q '^API_PORT=' "$ENV_FILE" 2>/dev/null; then
  sed -i "s/^API_PORT=.*/API_PORT=$API_PORT/" "$ENV_FILE"
else
  echo "API_PORT=$API_PORT" >> "$ENV_FILE"
fi
grep -q '^API_HOST=' "$ENV_FILE" || echo 'API_HOST=0.0.0.0' >> "$ENV_FILE"

# 初始化 DuckDB（生成 data/quant.duckdb 及表结构）
echo "==> 初始化 DuckDB 数据库..."
sudo -u "$APP_USER" bash -c "
  set -euo pipefail
  cd '$APP_DIR'
  source .venv/bin/activate
  export PYTHONPATH='$APP_DIR'
  set -a && source .env && set +a
  python -c 'from src.data.models import init_db; init_db(); print(\"init_db ok\")'
"

# systemd
SVC_DST=/etc/systemd/system/quant-ai.service
sed -e "s|/opt/ai-quant-system|$APP_DIR|g" \
    -e "s|User=quant|User=$APP_USER|g" \
    -e "s|Group=quant|Group=$APP_USER|g" \
    "$APP_DIR/scripts/deploy/quant-ai.service" > "$SVC_DST"

systemctl daemon-reload
systemctl enable quant-ai.service

# 防火墙提示（ufw 若启用需放行）
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q active; then
  echo "==> 检测到 ufw 已启用，放行 TCP $API_PORT ..."
  ufw allow "$API_PORT/tcp" || true
fi

echo ""
echo "安装完成。"
echo "  启动:  sudo systemctl start quant-ai"
echo "  状态:  sudo systemctl status quant-ai"
echo "  日志:  sudo journalctl -u quant-ai -f"
echo "  看板:  http://<服务器IP>:$API_PORT"
echo ""
echo "若从本机同步了 data/quant.duckdb，请执行:"
echo "  sudo chown -R $APP_USER:$APP_USER $APP_DIR/data"
