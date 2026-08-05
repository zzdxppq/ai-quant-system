# Ubuntu 部署（端口 8001）

代码通过 **GitHub 仓库** 在服务器上 `git clone` / `git pull`，无需本机 scp/rsync。

## 架构说明

- **DuckDB**：`pip install duckdb duckdb-engine` 即可；首次 `init_db()` 生成 `data/quant.duckdb`。
- **端口**：`.env` 中 `API_PORT=8001`（见 `ubuntu.env.example`）。

---

## 一、服务器首次部署

```bash
# 1. 克隆仓库（按你的仓库地址修改）
sudo mkdir -p /opt/ai-quant-system
sudo git clone https://github.com/<你的用户名>/ai-quant-system.git /opt/ai-quant-system
# 或 SSH: git clone git@github.com:<你的用户名>/ai-quant-system.git /opt/ai-quant-system

cd /opt/ai-quant-system

# 2. 安装环境 + systemd（Python 3.12、venv、DuckDB、init_db）
sudo bash scripts/deploy/ubuntu_install.sh

# 3. 启动
sudo systemctl start quant-ai
sudo systemctl status quant-ai
```

看板：`http://<服务器IP>:8001`

云安全组 / `ufw` 放行 **TCP 8001**。

---

## 二、日常更新（GitHub 同步后）

```bash
cd /opt/ai-quant-system
sudo -u quant git pull
# 若 requirements.txt 有变：
sudo -u quant bash -c 'cd /opt/ai-quant-system && source .venv/bin/activate && pip install -r requirements.txt'
sudo systemctl restart quant-ai
```

仅改 `.env` 时：`sudo systemctl restart quant-ai` 即可。

---

## 三、运维命令

```bash
sudo systemctl start|stop|restart quant-ai
sudo systemctl status quant-ai
sudo journalctl -u quant-ai -f
```

---

## 四、可选：迁移本机 DuckDB 数据

本机 `data/quant.duckdb` 可单独传到服务器（勿提交 Git）：

```bash
# 在服务器上，上传后：
sudo chown -R quant:quant /opt/ai-quant-system/data
sudo systemctl restart quant-ai
```

---

## 五、环境变量

```bash
cp scripts/deploy/ubuntu.env.example .env
# 编辑 SMTP 等后
sudo chown quant:quant .env
sudo systemctl restart quant-ai
```

安装脚本会确保 `API_PORT=8001`。

---

## 六、自定义路径 / 用户

```bash
sudo APP_DIR=/home/ubuntu/ai-quant APP_USER=ubuntu API_PORT=8001 \
  bash scripts/deploy/ubuntu_install.sh
```

同时修改 `quant-ai.service` 中的路径，或让安装脚本自动替换（已支持 `APP_DIR`）。

---

## Docker 部署（推荐，与 GitHub 配合）

镜像内已包含 **Python 3.12 + DuckDB(pip)**，对外端口 **8001**。数据用 Docker 卷持久化。

### 方式 A：在 Ubuntu 上 git pull 后构建（最简单）

```bash
git clone https://github.com/<你>/ai-quant-system.git /opt/ai-quant-system
cd /opt/ai-quant-system

cp scripts/deploy/ubuntu.env.example .env   # 可选，SMTP 等
# 若无 .env：docker compose 会报错，可 touch .env 或复制 example

sudo apt-get install -y docker.io docker-compose-plugin   # 或 docker-ce + compose plugin
sudo docker compose build
sudo docker compose up -d

sudo docker compose ps
sudo docker compose logs -f
```

访问：`http://<服务器IP>:8001`

更新代码：

```bash
cd /opt/ai-quant-system
git pull
sudo docker compose build
sudo docker compose up -d
```

### 方式 B：Windows 本地构建镜像，传到 Ubuntu（无镜像仓库）

```powershell
cd E:\claude\ai-quant-system
.\scripts\deploy\docker-save-load.ps1
scp dist\ai-quant-system.tar ubuntu@服务器:/tmp/
```

服务器：

```bash
sudo docker load -i /tmp/ai-quant-system.tar
cd /opt/ai-quant-system   # 仍需 git clone 拿 docker-compose.yml、.env 模板
cp scripts/deploy/ubuntu.env.example .env
sudo docker compose up -d    # 使用已 load 的 ai-quant-system:latest
```

### 方式 C：阿里云 ACR（本机构建 → 推送 → 服务器拉取）

**本机 Windows：**

```powershell
cd E:\claude\ai-quant-system
$REMOTE = "registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant:20260520.2211"

docker buildx build --platform linux/amd64 -t ai-quant-system:latest --load .
docker tag ai-quant-system:latest $REMOTE
docker login registry.cn-hangzhou.aliyuncs.com
docker push $REMOTE
```

**服务器：**

```bash
mkdir -p /opt/ai-quant-system/data /opt/ai-quant-system/logs
cd /opt/ai-quant-system
# 放入 docker-compose.prod.yml、.env、data/quant.duckdb
cp scripts/deploy/ubuntu.env.example .env   # 或 scp 本机 .env

sudo docker login registry.cn-hangzhou.aliyuncs.com
sudo docker compose -f docker-compose.prod.yml pull
sudo docker compose -f docker-compose.prod.yml up -d
```

镜像示例：`registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant:20260520.2211`（发新版时改 tag 并同步改 `docker-compose.prod.yml` 的 `image:`）。

### 方式 D：推送到 Docker Hub / GHCR

```bash
docker tag ai-quant-system:latest ghcr.io/<你>/ai-quant-system:latest
docker push ghcr.io/<你>/ai-quant-system:latest
```

服务器 `docker-compose.yml` 里把 `build:` 改成 `image: ghcr.io/<你>/ai-quant-system:latest` 后 `docker compose pull && up -d`。

### 使用本机 DuckDB 文件（绑定目录）

编辑 `docker-compose.yml` 卷为：

```yaml
volumes:
  - ./data:/app/data
  - ./logs:/app/logs
```

删掉顶部的 `quant-data` / `quant-logs` 命名卷定义。把本机 `data/quant.duckdb` 放到服务器项目 `data/` 下再启动。

### 注意

- **不要** `docker compose up --scale 2`：DuckDB 单写连接，只能单容器。
- 与 **systemd 直跑** 二选一，不要同时占 8001。
- 服务器需安装 Docker Engine（20.10+）及 Compose 插件。
- **卷挂载**：生产只需 `data/`、`logs/`（及宿主机 `.env`）；`scripts/`、`src/` 已 `COPY` 进镜像，**勿** `-v /opt/ai-quant-system:/app` 覆盖容器内代码。运维脚本用 `docker run --rm … python -u scripts/xxx.py` 即可。
