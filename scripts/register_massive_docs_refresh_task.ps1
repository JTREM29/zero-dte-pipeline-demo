param(
    [string]$TaskName = 'TNT_MassiveDocs_Refresh',
    [string]$At = '05:05',
    [string]$BaseUrl = $env:MASSIVE_DOCS_BASE_URL,
    [string[]]$Sections = @('rest','rest/options','rest/stocks','websocket'),
    [int]$MaxEndpoints = 200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$refreshScript = Join-Path $repoRoot 'scripts\refresh_massive_docs.ps1'
if (-not (Test-Path -LiteralPath $refreshScript)) {
    throw "Missing $refreshScript"
}

if (-not $BaseUrl -or -not $BaseUrl.Trim()) {
    $BaseUrl = 'https://massive.com'
}

# Build a robust command that runs in the repo root.
$escapedRepo = $repoRoot.Replace("'", "''")
$escapedRefresh = $refreshScript.Replace("'", "''")

$sectionsArg = ($Sections | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','

$psCommand = @(
    "Set-Location '$escapedRepo';",
    "& '$escapedRefresh' -BaseUrl '$($BaseUrl.Trim().TrimEnd('/').Replace("'","''"))' -Sections @($sectionsArg) -MaxEndpoints $MaxEndpoints"
) -join ' '

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -Command $psCommand"

# Daily trigger at local time.
$time = [DateTime]::ParseExact($At, 'HH:mm', $null)
$trigger = New-ScheduledTaskTrigger -Daily -At $time.TimeOfDay

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[OK] Registered scheduled task '$TaskName' at $At daily." -ForegroundColor Green
Write-Host "      Runs: scripts/refresh_massive_docs.ps1" -ForegroundColor DarkGray
