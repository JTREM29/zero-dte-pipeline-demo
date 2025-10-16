param(
    [string]$TaskName = "ZeroDTE_NewsWatch",
    [string]$Time = "09:00",
    [switch]$Weekdays,
    [int]$Minutes = 60,
    [switch]$FilterFly,
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [int]$ExecutionTimeLimitSec = 420 # default 7 minutes for a 5-minute watch window
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $RepoPath)) { throw "Repo path not found: $RepoPath" }
$script = Join-Path $RepoPath "scripts/start_news_watch.ps1"
if (-not (Test-Path -LiteralPath $script)) { throw "start_news_watch.ps1 not found at $script" }

if ($Time -match '^(\d{1,2}):(\d{2})$') { $h = [int]$Matches[1]; $m = [int]$Matches[2]; $atTime = Get-Date -Hour $h -Minute $m -Second 0 } else { $atTime = Get-Date -Hour 9 -Minute 0 -Second 0 }

$psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script + '"'))
$psArgs += @('-Minutes', [string]$Minutes)
if ($FilterFly) { $psArgs += @('-FilterFly') }
$argString = ($psArgs -join ' ')

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString -WorkingDirectory $RepoPath
if ($Weekdays) { $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $atTime } else { $trigger = New-ScheduledTaskTrigger -Daily -At $atTime }
$userId = if ($env:UserDomain) { "$env:UserDomain\$env:UserName" } else { $env:UserName }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive
$limit = if ($ExecutionTimeLimitSec -lt 60) { 60 } else { $ExecutionTimeLimitSec }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds $limit)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Task '$TaskName' registered at $Time (Minutes $Minutes)."
