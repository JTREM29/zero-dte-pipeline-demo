param(
    [string]$Time = '08:55',
    [switch]$Weekdays,
    [switch]$RequireAdmin,
    [int]$ExecutionTimeLimitSec = 900  # 15 minutes default guardrail
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..')
Set-Location $repoRoot

$taskName = 'ZeroDTE_MorningBrief'
$ps1 = Join-Path $scriptDir 'run_morning_brief.ps1'
if (-not (Test-Path -LiteralPath $ps1)) { throw "Missing $ps1" }

# Build the action to run PowerShell with ExecutionPolicy Bypass
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ps1`""

# Build the trigger
$now = Get-Date
$et = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$local = [TimeZoneInfo]::ConvertTime($now, $et)
$start = [datetime]::ParseExact($Time, 'HH:mm', $null)
$start = [datetime]::SpecifyKind((Get-Date -Year $local.Year -Month $local.Month -Day $local.Day -Hour $start.Hour -Minute $start.Minute -Second 0), 'Local')
if ($Weekdays) {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $start
}
else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $start
}

# Register or update
try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}
catch {}
if ($ExecutionTimeLimitSec -lt 60) { $ExecutionTimeLimitSec = 60 }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds $ExecutionTimeLimitSec) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($RequireAdmin) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Generate SPX morning brief before market open' -RunLevel Highest | Out-Null
}
else {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Generate SPX morning brief before market open' | Out-Null
}
Write-Host "Task '$taskName' registered for $Time" -ForegroundColor Green
