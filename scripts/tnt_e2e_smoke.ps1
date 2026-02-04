param(
    [string]$Symbol = "SPY",
    [string]$Tf = "1m",
    [int]$TimeoutSec = 45,
    [int]$PostedWaitSec = 25,
    [switch]$UseCanary,
    [switch]$NoDiscord,
    [switch]$Verbose
)

$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot
Set-Location ..

function Write-Section([string]$Title) {
    Write-Host "";
    Write-Host "=== $Title ===";
}

function Fail([string]$Msg) {
    Write-Host "[FAIL] $Msg";
    exit 1
}

function Pass([string]$Msg) {
    Write-Host "[OK] $Msg";
}

$py = Join-Path (Resolve-Path '.') '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Fail "missing python venv at $py"
}

Write-Section "Preflight"
& $py -u scripts\redis_llen_once.py

$slashLog = Join-Path (Resolve-Path '.') 'logs\slash_live.log'
$workerLog = Join-Path (Resolve-Path '.') 'logs\redis_worker_live.log'

if (-not (Test-Path -LiteralPath $slashLog)) {
    Write-Host "[WARN] missing $slashLog (Discord bot may not be running via task)"
}
if (-not (Test-Path -LiteralPath $workerLog)) {
    Write-Host "[WARN] missing $workerLog (worker may not be running via task)"
}

Write-Section "Service sanity (processes)"
$rx = 'massive_service\.redis_worker|cli\.discord_bot|run_alert_scheduler_5m\.py|context_writer_loop\.py'
$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $rx) }
if (-not $procs) {
    Write-Host "[WARN] no matching python service processes found."
    Write-Host "      Start tasks: Redis worker (bg), Discord bot (bg), Alerts schedulers (enqueue), Context writer (bg)."
} else {
    $procs | Select-Object ProcessId, CommandLine | Format-Table -AutoSize
    Pass "services appear running (at least one matched)"
}

if ($NoDiscord) {
    Write-Section "Greenlight E2E (NO DISCORD)"
    Pass "Skipping Discord post check (--NoDiscord)"
    exit 0
}

Write-Section "Greenlight E2E (enqueue -> worker -> discord)"
$channel = $null

if ($UseCanary) {
    if ($env:DISCORD_CANARY_CHANNEL_ID) { $channel = $env:DISCORD_CANARY_CHANNEL_ID }
    elseif ($env:ALERTS_TEST_CHANNEL_ID) { $channel = $env:ALERTS_TEST_CHANNEL_ID }
    elseif ($env:TNT_ALERTS_CHANNEL_ID) { $channel = $env:TNT_ALERTS_CHANNEL_ID }
} else {
    if ($env:TNT_ALERTS_CHANNEL_ID) { $channel = $env:TNT_ALERTS_CHANNEL_ID }
    elseif ($env:ALERTS_CHANNEL_ID) { $channel = $env:ALERTS_CHANNEL_ID }
}

# Best-effort: infer channel_id from the bot startup line if env is missing.
if (-not $channel -and (Test-Path -LiteralPath $slashLog)) {
    try {
        $tail = Get-Content -LiteralPath $slashLog -Tail 250
        $enabled = $tail | Select-String -Pattern 'DELIVERY\] Enabled:.*channel_id=\d+' | Select-Object -Last 1
        if ($enabled -and ($enabled.Line -match 'channel_id=(\d+)')) {
            $channel = $Matches[1]
            if ($Verbose) { Write-Host "[INFO] inferred channel_id=$channel from slash_live.log" }
        }
    } catch {
        # ignore
    }
}

if (-not $channel) {
    Fail "Missing channel id. Set DISCORD_CANARY_CHANNEL_ID (recommended) or TNT_ALERTS_CHANNEL_ID; or start Discord bot (bg) so we can infer it from logs/slash_live.log."
}

# Run a safe single-trigger greenlight and capture its output.
$start = Get-Date
$greenOut = & $py -u scripts\alerts_greenlight_e2e.py --symbol $Symbol --tf $Tf --seconds $TimeoutSec --poll-sec 1 --expires-min 2 --cooldown-sec 1 --session CUSTOM --enqueue --stop-after-first-trigger --channel-id $channel 2>&1
$greenText = ($greenOut | Out-String)
Write-Host $greenText

# Extract alert id from output.
$alertId = $null
if ($greenText -match 'Created\s+(A\d{8})') { $alertId = $Matches[1] }
if (-not $alertId) {
    Fail "Could not parse alert_id from greenlight output"
}
Pass "greenlight created $alertId and enqueued"

# Wait for a POSTED line mentioning this alert_id.
if (-not (Test-Path -LiteralPath $slashLog)) {
    Fail "missing $slashLog; cannot confirm POSTED. Start 'Discord bot (bg)' task."
}

$deadline = (Get-Date).AddSeconds([math]::Max(5, $PostedWaitSec))
$found = $false
while ((Get-Date) -lt $deadline) {
    try {
        $tail = Get-Content -LiteralPath $slashLog -Tail 500
        # POSTED lines usually contain job_id=alert_trigger:<ALERT_ID>:...
        $hit = $tail | Select-String -Pattern ("DELIVERY\]\[POSTED\].*" + [regex]::Escape($alertId))
        if ($hit) { $found = $true; break }
    } catch {
        # ignore transient file locks
    }
    Start-Sleep -Seconds 1
}

if (-not $found) {
    Write-Host "[WARN] did not observe POSTED for alert_id=$alertId within ${PostedWaitSec}s"
    Write-Host "       Quick hints: confirm worker running; confirm TNT_ALERTS_DISCORD_DELIVERY_ENABLED=1; check logs/slash_live.log for errors."
    exit 2
}

Pass "Discord POSTED observed for $alertId"

Write-Section "Snapshot"
& $py -u scripts\redis_llen_once.py
Write-Host "Done."
exit 0
