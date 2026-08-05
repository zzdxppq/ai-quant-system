# 结束占用 quant.duckdb / 8000 的旧服务并重新启动 main.py
param(
    [int]$Port = 8000,
    [string]$Python = ""
)

$ErrorActionPreference = "SilentlyContinue"
$root = (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$env:PYTHONPATH = $root
$env:NO_PROXY = "*"
$env:no_proxy = "*"

Write-Host "正在结束旧服务 (kill_quant) ..."
& (Join-Path $root "scripts\kill_quant.ps1")
Start-Sleep -Seconds 2

Write-Host "启动服务 (Python 3.12) ..." -ForegroundColor Cyan
& (Join-Path $root "scripts\start_server.ps1") -Port $Port -Python $Python -NewWindow
