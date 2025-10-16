<#
Registers a per-user Scheduled Task to run the morning runner daily with pre-smoke, premarket, and open-to-close.

Defaults:
- Symbols: SPY,QQQ,IWM
- PreSmoke: 08:58 for 1 minute @ 1s
- Premarket: 09:00 for 30 minutes
- Open: 09:30 until 16:00 (computed)
- Device: dml
- RunWatcher: enabled (for notifications)
#>

param(
    [string]$TaskName = "ZeroDTE_MorningRunner",
    [string[]]$Symbols = @("SPX"),
    [string]$PremarketTime = "09:00",
    [string]$OpenTime = "09:30",
    [string]$OpenEndTime = "16:00",
    [string]$PreSmokeTime = "08:58",
    [double]$IntervalSec = 10.0,
    [int]$PremarketMinutes = 30,
    [int]$OpenMinutes = 120,
    [switch]$RunWatcher,
    [string]$Device = "dml",
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [int]$ExecutionTimeLimitSec = 21600 # 6 hours default
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $RepoPath)) { throw "Repo path not found: $RepoPath" }
$runner = Join-Path $RepoPath "scripts\morning_runner.ps1"
if (-not (Test-Path -LiteralPath $runner)) { throw "morning_runner.ps1 not found at $runner" }

# Build arguments to powershell.exe
$argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $runner + '"'))
$argList += @('-Symbols', ('"' + ($Symbols -join ',') + '"'))
$argList += @('-IntervalSec', [string]$IntervalSec)
$argList += @('-PremarketMinutes', [string]$PremarketMinutes)
$argList += @('-OpenMinutes', [string]$OpenMinutes)
$argList += @('-PremarketTime', ('"' + $PremarketTime + '"'))
$argList += @('-OpenTime', ('"' + $OpenTime + '"'))
$argList += @('-OpenEndTime', ('"' + $OpenEndTime + '"'))
$argList += @('-Device', ('"' + $Device + '"'))
$argList += @('-PreSmoke')
$argList += @('-PreSmokeTime', ('"' + $PreSmokeTime + '"'))
$argList += @('-PreSmokeSymbols', ('"' + ($Symbols -join ',') + '"'))
if ($RunWatcher) { $argList += @('-RunWatcher') }

$argString = ($argList -join ' ')

# Trigger: daily at 08:58 to allow pre-smoke and subsequent waits
$atTime = Get-Date -Hour 8 -Minute 58 -Second 0
$trigger = New-ScheduledTaskTrigger -Daily -At $atTime

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString -WorkingDirectory $RepoPath
$userId = if ($env:UserDomain) { "$env:UserDomain\$env:UserName" } else { $env:UserName }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive
$limit = if ($ExecutionTimeLimitSec -lt 1800) { 1800 } else { $ExecutionTimeLimitSec }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds $limit)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task '$TaskName' registered for daily 08:58 with symbols: $($Symbols -join ', ')"
Write-Host "Working directory: $RepoPath"
Write-Host "Argument: $argString"
