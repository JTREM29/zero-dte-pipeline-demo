param(
    [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe"
)

$ErrorActionPreference = 'Continue'

function Stop-PythonByPattern {
    param(
        [Parameter(Mandatory = $true)][string]$Pattern,
        [Parameter(Mandatory = $true)][string]$Role
    )

    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $Pattern)
    }

    foreach ($p in $procs) {
        try {
            Write-Output "[OPS] stopping $Role pid=$($p.ProcessId)"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
            # non-fatal
        }
    }
}

function Test-FuturesConnected {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExePath
    )
    try {
        & $PythonExePath -u "$repoRoot\scripts\futures_health_databento.py" | ForEach-Object { Write-Output "[FUTURES_HEALTH] $_" }
        return ($LASTEXITCODE -eq 0)
    } catch {
        Write-Output "[FUTURES_HEALTH][WARN] exception: $($_.Exception.Message)"
        return $false
    }
}

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$logsDir = "$repoRoot\logs"
if (-not (Test-Path -LiteralPath $logsDir)) {
    New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
}

Write-Output "[OPS] restart(v2): discord + databento"
Write-Output "[OPS] repo=$repoRoot"

# Stop bots + futures ingest (both REAL and launcher/shim processes)
Stop-PythonByPattern -Pattern '(-m\s+cli\.discord_bot)|(\bcli\.discord_bot\b)' -Role 'SLASH_BOT'
Stop-PythonByPattern -Pattern '(-m\s+delivery\.discord_bot)|(\bdelivery\.discord_bot\b)' -Role 'DELIVERY_BOT'
Stop-PythonByPattern -Pattern '(futures_ingest_databento\.py)|(scripts\\futures_ingest_databento\.py)' -Role 'FUTURES_INGEST'

# Clear stale lock breadcrumbs (best-effort, no Join-Path)
$lockFiles = @(
    "$logsDir\tnt_discord_bot.cli.discord_bot.lock",
    "$logsDir\tnt_discord_bot.delivery.discord_bot.lock",
    "$logsDir\tnt_discord_bot.lock",
    "$logsDir\tnt_futures_ingest.lock",
    "$logsDir\futures_ingest.pid"
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
Write-Output "[OPS] starting futures ingest (attempt=1)"
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_futures_ingest_databento_bg.ps1" -PythonExe $PythonExe

$waitSec = 12
$pollSec = 2
$deadline = (Get-Date).AddSeconds($waitSec)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $pollSec
    if (Test-FuturesConnected -PythonExePath $PythonExe) { $ok = $true; break }
}

if (-not $ok) {
    Write-Output "[OPS][WARN] futures not connected after ${waitSec}s; restarting futures ingest once"
    Stop-PythonByPattern -Pattern '(futures_ingest_databento\.py)|(scripts\\futures_ingest_databento\.py)' -Role 'FUTURES_INGEST'
    Start-Sleep -Seconds 1
    Write-Output "[OPS] starting futures ingest (attempt=2)"
    powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_futures_ingest_databento_bg.ps1" -PythonExe $PythonExe

    $deadline2 = (Get-Date).AddSeconds($waitSec)
    while ((Get-Date) -lt $deadline2) {
        Start-Sleep -Seconds $pollSec
        if (Test-FuturesConnected -PythonExePath $PythonExe) { $ok = $true; break }
    }
}

if (-not $ok) {
    Write-Output "[OPS][ACTION REQUIRED] futures still CONNECTED=0 after retry; check DATABENTO_API_KEY / network / logs/futures_ingest_stderr.log"
    exit 2
}

# Start slash bot + delivery bot (detached)
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_slash_bot_bg.ps1" -PythonExe $PythonExe
powershell -NoProfile -ExecutionPolicy Bypass -File "$repoRoot\scripts\start_delivery_bot_bg.ps1" -PythonExe $PythonExe

Write-Output "[OPS] restart(v2) complete (processes launched detached)"
