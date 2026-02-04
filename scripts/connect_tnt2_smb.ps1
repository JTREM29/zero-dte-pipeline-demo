param(
    [string]$HostOrIp = 'TNT2',
    [string]$JobsShareName = 'tnt_jobs',
    [string]$ResultsShareName = 'tnt_results',
    [switch]$KeepDrives
)

$ErrorActionPreference = 'Stop'

function Write-Section([string]$Title) {
    Write-Host "=== $Title ==="
}

$jobs = "\\$HostOrIp\$JobsShareName"
$results = "\\$HostOrIp\$ResultsShareName"

Write-Section "Target"
Write-Host "jobs:    $jobs"
Write-Host "results: $results"

Write-Section "Clear existing SMB sessions (best-effort)"
try { net use $jobs /delete /y | Out-Null } catch {}
try { net use $results /delete /y | Out-Null } catch {}
try { net use "\\$HostOrIp" /delete /y | Out-Null } catch {}

Write-Section "Enter credentials"
Write-Host "A credential prompt will appear (password input hidden)."
$cred = Get-Credential -Message "Credentials for $HostOrIp SMB shares ($JobsShareName/$ResultsShareName)"

Write-Section "Connect (temporary PS drives)"
# Using PSDrives avoids putting the password on the command line.
$driveJobs = 'TNTJ'
$driveResults = 'TNTR'

# Clean up if something is already mapped.
Get-PSDrive -Name $driveJobs -ErrorAction SilentlyContinue | ForEach-Object { Remove-PSDrive -Name $driveJobs -Force -ErrorAction SilentlyContinue }
Get-PSDrive -Name $driveResults -ErrorAction SilentlyContinue | ForEach-Object { Remove-PSDrive -Name $driveResults -Force -ErrorAction SilentlyContinue }

New-PSDrive -Name $driveJobs -PSProvider FileSystem -Root $jobs -Credential $cred -ErrorAction Stop | Out-Null
New-PSDrive -Name $driveResults -PSProvider FileSystem -Root $results -Credential $cred -ErrorAction Stop | Out-Null

Write-Section "Verify"
$okJobs = Test-Path -LiteralPath $jobs
$okResults = Test-Path -LiteralPath $results
Write-Host "Test-Path jobs:    $okJobs"
Write-Host "Test-Path results: $okResults"

if (-not $okJobs -or -not $okResults) {
    throw "SMB reachable but share not accessible (jobs=$okJobs results=$okResults). Check share permissions and credentials."
}

$probeName = "_controller_probe_$(Get-Date -Format yyyyMMdd_HHmmss).txt"
$probePath = Join-Path $jobs $probeName
"ok" | Set-Content -LiteralPath $probePath -Encoding utf8 -ErrorAction Stop
Write-Host "Write probe OK: $probePath"

if (-not $KeepDrives) {
    Write-Section "Cleanup temp drives"
    Remove-PSDrive -Name $driveJobs -Force -ErrorAction SilentlyContinue
    Remove-PSDrive -Name $driveResults -Force -ErrorAction SilentlyContinue
    Write-Host "Temp drives removed. SMB session may still persist for this logon session."
} else {
    Write-Host "Keeping PS drives: $driveJobs ($jobs), $driveResults ($results)"
}

Write-Host "OK"
