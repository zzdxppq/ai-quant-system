# 本地构建 linux/amd64 镜像并导出 tar，拷到 Ubuntu 后 docker load（无镜像仓库时）
# 用法:
#   .\scripts\deploy\docker-save-load.ps1
#   scp dist/ai-quant-system.tar ubuntu@服务器:/tmp/
# 服务器:
#   docker load -i /tmp/ai-quant-system.tar
#   cd /opt/ai-quant-system && docker compose up -d

param(
    [string]$ImageTag = "ai-quant-system:latest",
    [string]$OutDir = "dist",
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $root

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tar = Join-Path $OutDir "ai-quant-system.tar"

Write-Host "构建 $Platform -> $ImageTag ..." -ForegroundColor Cyan
docker buildx build --platform $Platform -t $ImageTag --load .

Write-Host "导出 $tar ..." -ForegroundColor Cyan
docker save -o $tar $ImageTag

Write-Host "完成。上传到服务器后:" -ForegroundColor Green
Write-Host "  docker load -i ai-quant-system.tar"
Write-Host "  cd /opt/ai-quant-system && docker compose up -d"
