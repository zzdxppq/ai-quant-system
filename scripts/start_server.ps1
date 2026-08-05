# Start ai-quant-system (foreground; Ctrl+C to stop)
# Usage:
#   .\scripts\start_server.ps1
#   .\scripts\start_server.ps1 -NewWindow
#   .\scripts\start_server.ps1 -Python "C:\...\Python312\python.exe" -NewWindow

param(
    [int]$Port = 8000,
    [string]$Python = "",
    [switch]$NewWindow
)

$ErrorActionPreference = "Stop"
try { chcp 65001 | Out-Null } catch {}
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$root = (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$env:PYTHONPATH = $root
$env:NO_PROXY = "*"
$env:no_proxy = "*"
$env:HTTP_PROXY = ""
$env:HTTPS_PROXY = ""
$env:http_proxy = ""
$env:https_proxy = ""
$env:TQDM_DISABLE = "1"
$env:OMP_NUM_THREADS = "1"
$env:OPENBLAS_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
$env:NUMEXPR_NUM_THREADS = "1"
if (-not $env:STOCK_BASIC_STARTUP_SYNC) { $env:STOCK_BASIC_STARTUP_SYNC = "0" }
if (-not $env:STARTUP_ADVICE_REFRESH) { $env:STARTUP_ADVICE_REFRESH = "0" }
# 首屏不拉 ranking-live 外网（看板 45s 后再轮询）；设为 0 可恢复实时 TOP30
if (-not $env:SKIP_LIVE_RANKING_FETCH) { $env:SKIP_LIVE_RANKING_FETCH = "1" }

function Find-Python312Exe {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Program Files\Python312\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    try {
        $out = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            return ($out | Select-Object -Last 1).Trim()
        }
    } catch {}
    throw "Python 3.12 not found. Install 3.12 or pass -Python with full path to python.exe."
}

function Resolve-PythonExe([string]$Spec) {
    $s = ($Spec -as [string]).Trim()
    if (-not $s) { return Find-Python312Exe }
    if ($s -match '\s') {
        $parts = $s -split '\s+', 2
        $out = & $parts[0] $parts[1] -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
        throw "Cannot resolve Python: $s"
    }
    if (Test-Path $s) { return (Resolve-Path $s).Path }
    $w = Get-Command $s -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    throw "Python not found: $s"
}

function Assert-PythonVersionOk([string]$Exe) {
    $verLine = & $Exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot read Python version: $Exe" }
    $verLine = ($verLine | Select-Object -Last 1).ToString().Trim()
    $maj = [int]($verLine.Split('.')[0])
    if ($maj -ge 13) {
        throw "Refuse Python $verLine ($Exe). Use 3.12: py -3.12 or -Python with Python312\python.exe"
    }
}

$pyExe = Resolve-PythonExe $Python
Assert-PythonVersionOk $pyExe
$pyVer = & $pyExe --version 2>&1 | Out-String
$pyVer = $pyVer.Trim()

$listen = "http://127.0.0.1:$Port"
Write-Host "Root: $root" -ForegroundColor Cyan
Write-Host "URL:  $listen" -ForegroundColor Cyan
Write-Host "Python: $pyExe ($pyVer)" -ForegroundColor Cyan
Write-Host "Tip: keep server in new window: .\scripts\start_server.ps1 -NewWindow" -ForegroundColor Yellow
Write-Host ""

if ($NewWindow) {
    # Pass resolved full path only (avoid "py -3.12" split into multiple argv tokens)
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $root "scripts\start_server.ps1"),
        "-Port", "$Port",
        "-Python", $pyExe
    ) -WorkingDirectory $root
    Write-Host "Started in a new window ($pyVer)." -ForegroundColor Green
    exit 0
}

& $pyExe main.py
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ""
    if ($code -eq -1073740791 -or $code -eq 3221226505) {
        Write-Host "[EXIT] Process crashed (native error, exit=$code / 0xC0000409)." -ForegroundColor Red
        Write-Host "       Often Python 3.14 + network libs. Use 3.12: .\scripts\start_server.ps1 -NewWindow" -ForegroundColor Yellow
    } else {
        Write-Host "[EXIT] Server stopped (exit=$code). Port busy? Run .\scripts\kill_quant.ps1 first." -ForegroundColor Red
    }
    Read-Host "Press Enter to close"
}
