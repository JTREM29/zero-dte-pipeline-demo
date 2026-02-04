param(
    [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe"
)

$ErrorActionPreference = 'Continue'

function Stop-PythonByPattern {
    param(
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $procs = Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $Pattern) }

    foreach ($p in $procs) {
        try {
            Write-Output "[OPS] stopping $Role pid=$($p.ProcessId)"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
            # non-fatal
        }
    }
}

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$logDir = (Resolve-Path -LiteralPath "$repoRoot\logs" -ErrorAction SilentlyContinue)
if (-not $logDir) {
    New-Item -ItemType Directory -Force -Path "$repoRoot\logs" | Out-Null
    $logDir = (Resolve-Path -LiteralPath "$repoRoot\logs")
}
$logDirAbs = $logDir.Path

Write-Output "[OPS] restart: discord + databento"
Write-Output "[OPS] repo=$repoRoot"

# Stop bots + futures ingest (both REAL and launcher/shim processes)
Stop-PythonByPattern -Pattern '(-m\s+cli\.discord_bot)|(\bcli\.discord_bot\b)' -Role 'SLASH_BOT'
Stop-PythonByPattern -Pattern '(-m\s+delivery\.discord_bot)|(\bdelivery\.discord_bot\b)' -Role 'DELIVERY_BOT'
Stop-PythonByPattern -Pattern '(futures_ingest_databento\.py)|(scripts\\futures_ingest_databento\.py)' -Role 'FUTURES_INGEST'

# Clear stale lock breadcrumbs (best-effort)
$lockFiles = @(
    "$logDirAbs\tnt_discord_bot.cli.discord_bot.lock"
    "$logDirAbs\tnt_discord_bot.delivery.discord_bot.lock"
    "$logDirAbs\tnt_discord_bot.lock"
    "$logDirAbs\tnt_futures_ingest.lock"
    "$logDirAbs\futures_ingest.pid"
)
foreach ($f in $lockFiles) {
    try {
        if (Test-Path -LiteralPath $f) {
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # non-fatal
    }
}

# Start Databento futures ingest (detached, with correct PYTHONPATH)
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_futures_ingest_databento_bg.ps1" -PythonExe $PythonExe | Out-String | Write-Output

# Start slash bot + delivery bot (detached)
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_slash_bot_bg.ps1" -PythonExe $PythonExe | Out-String | Write-Output
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_delivery_bot_bg.ps1" -PythonExe $PythonExe | Out-String | Write-Output

Write-Output "[OPS] restart complete (processes launched detached)"