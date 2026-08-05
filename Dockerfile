# ai-quant-system — Python 3.12 + DuckDB(pip) + 看板/定时任务
# 构建（Ubuntu 服务器多为 amd64）:
#   docker build -t ai-quant-system:latest .
# Windows 交叉构建:
#   docker buildx build --platform linux/amd64 -t ai-quant-system:latest --load .
#
# 默认基础镜像走 DaoCloud 代理（国内访问 docker.io 常超时）。
# 可直连 Docker Hub 时覆盖：
#   docker build --build-arg BASE_IMAGE=python:3.12-slim-bookworm -t ... .
#
# 不跑 apt-get：国内 deb/阿里云源常 403/超时；时区用 pip tzdata。
# pip 多镜像回退：单源常返回空索引（versions: none）。

ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    API_HOST=0.0.0.0 \
    API_PORT=8001 \
    DATA_STORAGE_BACKEND=quant \
    STOCK_BASIC_STARTUP_SYNC=0 \
    STARTUP_ADVICE_REFRESH=0 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    TQDM_DISABLE=1 \
    NO_PROXY=* \
    no_proxy=*

COPY requirements.txt .
# 依次尝试国内镜像；阿里云偶发 403/空索引时不阻断构建
RUN set -eux; \
    ok=0; \
    for idx in \
      "https://pypi.tuna.tsinghua.edu.cn/simple" \
      "https://mirrors.cloud.tencent.com/pypi/simple" \
      "https://mirrors.aliyun.com/pypi/simple" \
      "https://pypi.org/simple"; do \
      echo ">>> pip index: $idx"; \
      host="$(echo "$idx" | sed -E 's|https?://||;s|/.*||')"; \
      if pip install --no-cache-dir \
          -i "$idx" \
          --trusted-host "$host" \
          -r requirements.txt; then \
        ok=1; \
        break; \
      fi; \
      echo ">>> failed: $idx"; \
    done; \
    test "$ok" -eq 1; \
    python -c "import duckdb, uvicorn, fastapi, zoneinfo; zoneinfo.ZoneInfo('Asia/Shanghai'); print('deps ok', duckdb.__version__)"

COPY . .

RUN mkdir -p /app/data /app/logs \
    && chmod +x /app/scripts/deploy/docker-entrypoint.sh

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/', timeout=5)" || exit 1

ENTRYPOINT ["/app/scripts/deploy/docker-entrypoint.sh"]
CMD ["python", "main.py"]
