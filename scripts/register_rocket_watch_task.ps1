param(
    [string]$TaskName = "ZeroDTE_RocketWatch",
    [string]$Time = "09:30",
    [switch]$Weekdays,
    [string]$EndTime = "16:00",
    [string]$Preset = "mimic_ex_post",
    [float]$Poll = 2.0,
    [float]$OtmPercent = 0.12,
    [float]$NearPct = 0.08,
    [float]$MinPrice = 0.05,
    [int]$BatchSize = 80,
    [int]$MaxCurated = 200,
    [float]$PctThreshCheap = 0.7,
    [float]$PctThresh = 0.4,
    [float]$UptickRatio = 0.5,
    [float]$WindowSec = 75.0,
    [float]$DebounceSec = 180.0,
    [string]$RecordTicks = "", 
    [switch]$Notify,
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [int]$ExecutionTimeLimitSec = 28800  # 8 hours default
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $RepoPath)) { throw "Repo path not found: $RepoPath" }
$script = Join-Path $RepoPath "scripts/start_rocket_watch.ps1"
if (-not (Test-Path -LiteralPath $script)) { throw "start_rocket_watch.ps1 not found at $script" }

if ($Time -match '^(\d{1,2}):(\d{2})$') { $h = [int]$Matches[1]; $m = [int]$Matches[2]; $atTime = Get-Date -Hour $h -Minute $m -Second 0 } else { $atTime = Get-Date -Hour 9 -Minute 30 -Second 0 }

$psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script + '"'))
$psArgs += @('-EndTime', ('"' + $EndTime + '"'))
$psArgs += @('-Preset', ('"' + $Preset + '"'))
# Favor size reco defaults; let regime logic decide VIX puts-only dynamically
$psArgs += @('-UseAlertSizeReco')
$psArgs += @('-PutPosTrendSize', '0')
$psArgs += @('-PutNegTrendSize', '0.25')
$psArgs += @('-CallNegTrendSize', '1.0')
$psArgs += @('-Poll', [string]$Poll)
$psArgs += @('-OtmPercent', [string]$OtmPercent)
$psArgs += @('-NearPct', [string]$NearPct)
$psArgs += @('-MinPrice', [string]$MinPrice)
$psArgs += @('-BatchSize', [string]$BatchSize)
$psArgs += @('-MaxCurated', [string]$MaxCurated)
$psArgs += @('-PctThreshCheap', [string]$PctThreshCheap)
$psArgs += @('-PctThresh', [string]$PctThresh)
$psArgs += @('-UptickRatio', [string]$UptickRatio)
$psArgs += @('-WindowSec', [string]$WindowSec)
$psArgs += @('-DebounceSec', [string]$DebounceSec)
if (-not $RecordTicks -or $RecordTicks.Trim().Length -eq 0) {
    # Let starter script resolve AUT O to a dated CSV using the chosen Expiry
    $RecordTicks = 'AUTO'
}
$psArgs += @('-RecordTicks', ('"' + $RecordTicks + '"'))
if ($Notify) { $psArgs += @('-Notify') }

# Use robust defaults tuned for live: directional + regime-aware + rescue on sharp downtrends during RTH
$psArgs += @('-LooseDirectional')
$psArgs += @('-AutoSelectRegime')
$psArgs += @('-AutoRescueDowntrend')
$psArgs += @('-RescueThresholdBp', '20')
$psArgs += @('-IncludeHours', '"09:30-16:00"')
$psArgs += @('-PreflightIQFeed')
$psArgs += @('-UsePolygonQuotes')
$psArgs += @('-MinDayVolume', '0')
# Enforce agent-only routing after the watcher completes
$psArgs += @('-AgentOnly')

$argString = ($psArgs -join ' ')

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString -WorkingDirectory $RepoPath
if ($Weekdays) { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $atTime } else { $trigger = New-ScheduledTaskTrigger -Daily -At $atTime }
$userId = if ($env:UserDomain) { "$env:UserDomain\$env:UserName" } else { $env:UserName }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive
$limit = if ($ExecutionTimeLimitSec -lt 600) { 600 } else { $ExecutionTimeLimitSec }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds $limit)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task '$TaskName' registered at $Time (EndTime $EndTime)."
