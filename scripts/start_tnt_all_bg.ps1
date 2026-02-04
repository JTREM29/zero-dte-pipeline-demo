param(
    [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe",
    [string]$LogDir = "$PSScriptRoot\..\logs",
    [switch]$NoNewWindow
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$py = (Resolve-Path -LiteralPath $PythonExe).Path

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logDirAbs = (Resolve-Path -LiteralPath $LogDir).Path

# Ensure module resolution matches repo runners.
$env:PYTHONPATH = '.'

# Safe defaults (do not override if already set)
if (-not $env:TNT_REDIS_HOST) { $env:TNT_REDIS_HOST = '127.0.0.1' }
if (-not $env:TNT_REDIS_PORT) { $env:TNT_REDIS_PORT = '6379' }
if (-not $env:TNT_REDIS_DB) { $env:TNT_REDIS_DB = '0' }

Write-Host "[OPS] TNT start-all (detached)" -ForegroundColor Cyan
Write-Host "[OPS] repo=$repoRoot" -ForegroundColor DarkGray

# Quick Redis reachability hint (non-fatal; keep it simple)
try {
    $ok = (Test-NetConnection -ComputerName $env:TNT_REDIS_HOST -Port ([int]$env:TNT_REDIS_PORT)).TcpTestSucceeded
    if (-not $ok) {
        Write-Host "[OPS][WARN] Redis not reachable at $($env:TNT_REDIS_HOST):$($env:TNT_REDIS_PORT) (Memurai may be stopped)" -ForegroundColor Yellow
    }
} catch {
    # ignore
}

$commonArgs = @{
    PythonExe = $py
    LogDir    = $logDirAbs
}
if ($NoNewWindow) { $commonArgs['NoNewWindow'] = $true }

# Start everything. These are detached processes writing into logs/.
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_futures_ingest_databento_bg.ps1" @commonArgs
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_redis_worker_bg.ps1" @commonArgs
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_news_poller_bg.ps1" @commonArgs
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_slash_bot_bg.ps1" @commonArgs
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_delivery_bot_bg.ps1" @commonArgs

Write-Host "[OPS] started all (detached). Next: run Ops: StrictTonight proof" -ForegroundColor Green
