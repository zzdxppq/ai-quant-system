# 结束占用 quant.duckdb / 8000 的本项目 python 服务（不启动新服务）
$ErrorActionPreference = "SilentlyContinue"
$root = (Split-Path -Parent $PSScriptRoot)
$rootNorm = $root.Replace('\', '/').ToLower()
$Port = 8000
$killed = @{}

function Stop-Pid([int]$procId, [string]$reason) {
    if ($killed.ContainsKey("$procId")) { return }
    $killed[$procId] = $true
    Write-Host "  $reason PID $procId"
    taskkill /PID $procId /F 2>$null | Out-Null
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        wmic process where "ProcessId=$procId" delete 2>$null | Out-Null
    }
}

Write-Host "结束 ai-quant-system 相关进程 (端口 $Port + main.py + duckdb) ..."

# 1) 释放 8000（旧服务可能只占 duckdb、命令行无项目路径）
foreach ($line in (netstat -ano | Select-String ":$Port\s")) {
    if ($line -notmatch "LISTENING") { continue }
    $parts = ($line.ToString().Trim() -split "\s+") | Where-Object { $_ }
    if ($parts.Count -lt 1) { continue }
    $procId = $parts[-1]
    if ($procId -match "^\d+$") {
        Stop-Pid ([int]$procId) "port $Port"
    }
}

# 2) python main.py：命令行含项目路径，或仅为 main.py（常见于 start_server 新窗口）
foreach ($p in Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue) {
    $cmd = [string]$p.CommandLine
    if (-not $cmd) { continue }
    $low = $cmd.ToLower()
    if ($low -notmatch "main\.py") { continue }
    $inProject = ($low -match [regex]::Escape($rootNorm)) -or ($low -match "ai-quant-system")
    $bareMain = ($low -match 'python\.exe"?\s+main\.py' -or $low -match 'python\.exe\s+main\.py')
    if (-not $inProject -and -not $bareMain) { continue }
    Stop-Pid ([int]$p.ProcessId) "main.py"
}

Start-Sleep -Seconds 2

$left = Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $c = [string]$_.CommandLine
        $c -match "main\.py" -and ($c -match "ai-quant-system" -or $c -match 'python\.exe"?\s+main\.py')
    }
if ($left) {
    Write-Host "警告: 仍有 python main.py:" -ForegroundColor Yellow
    $left | ForEach-Object { Write-Host "  PID $($_.ProcessId) $($_.CommandLine)" }
} else {
    Write-Host "完成。可执行: .\scripts\start_server.ps1 -NewWindow"
}
