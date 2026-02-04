# Schedules the market ingest loop to run automatically during U.S. market hours.
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\schedule_polygon_ingest.ps1

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$ingestScript = Join-Path $repoRoot "run_ingest.ps1"
$logPath = Join-Path $repoRoot "logs\market_ingest.log"

if (-not (Test-Path $ingestScript)) {
    throw "Missing ingest launcher: $ingestScript"
}

$taskName = "TNT-Market-Ingest"
$startTime = [DateTime]::Today.AddHours(9).AddMinutes(28)
if ($startTime -lt (Get-Date)) {
    # If we already passed today's start time, schedule for tomorrow
    $startTime = $startTime.AddDays(1)
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ingestScript`" *> `"$logPath`""
$trigger = New-ScheduledTaskTrigger -Daily -At $startTime
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 7) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:UserName -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Start market ingest loop during RTH" -Force

Write-Host "[OK] Scheduled task '$taskName' to run daily at $($startTime.ToShortTimeString()) local time."
Write-Host "[OK] Execution time limit set to 7 hours (auto-stops near 4:30 PM ET)."