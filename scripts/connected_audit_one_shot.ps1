param(
    [string]$RepoRoot = "$env:USERPROFILE\OneDrive\Desktop\ZeroDTE-pipeline",
    [string]$PythonExe = "$env:USERPROFILE\OneDrive\Desktop\ZeroDTE-pipeline\.venv\Scripts\python.exe"
)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
$py = (Resolve-Path -LiteralPath $PythonExe).Path

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return $null }
        $lines = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue
        foreach ($raw in $lines) {
            $line = ("$raw").Trim()
            if (-not $line) { continue }
            if ($line.StartsWith('#')) { continue }
            if ($line -notmatch '=') { continue }
            $k, $v = $line.Split('=', 2)
            if ($k.Trim() -ne $Key) { continue }
            $val = $v.Trim()
            if ($val.Length -ge 2) {
                if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                    $val = $val.Substring(1, $val.Length - 2)
                }
            }
            return $val.Trim()
        }
    } catch {
        return $null
    }
    return $null
}

function Get-Setting {
    param(
        [Parameter(Mandatory = $true)][string[]]$Keys,
        [Parameter(Mandatory = $true)][string]$Root
    )
    foreach ($k in $Keys) {
        try {
            $v = (Get-Item -Path ("Env:$k") -ErrorAction SilentlyContinue).Value
            if (-not [string]::IsNullOrWhiteSpace($v)) {
                return [pscustomobject]@{ Value = $v.Trim(); Source = "ENV:$k" }
            }
        } catch { }
    }

    $envLocal = Join-Path $Root '.env.local'
    $envFile = Join-Path $Root '.env'
    foreach ($k in $Keys) {
        $v = Get-DotEnvValue -Path $envLocal -Key $k
        if (-not [string]::IsNullOrWhiteSpace($v)) {
            return [pscustomobject]@{ Value = $v.Trim(); Source = ".env.local:$k" }
        }
        $v = Get-DotEnvValue -Path $envFile -Key $k
        if (-not [string]::IsNullOrWhiteSpace($v)) {
            return [pscustomobject]@{ Value = $v.Trim(); Source = ".env:$k" }
        }
    }

    return [pscustomobject]@{ Value = ""; Source = "NONE" }
}

$ask = Get-Setting -Keys @('TNT_ASK_CHANNEL_ID', 'ASK_TNT_CHANNEL_ID') -Root $root
$cal = Get-Setting -Keys @('TNT_CALENDAR_EARNINGS_CHANNEL_ID', 'CALENDAR_EARNINGS_CHANNEL_ID') -Root $root
$canary = Get-Setting -Keys @('DISCORD_CANARY_CHANNEL_ID', 'TNT_CANARY_CHANNEL_ID') -Root $root

$askVal = if ([string]::IsNullOrWhiteSpace($ask.Value)) { '-' } else { $ask.Value }
$calVal = if ([string]::IsNullOrWhiteSpace($cal.Value)) { '-' } else { $cal.Value }
$canaryVal = if ([string]::IsNullOrWhiteSpace($canary.Value)) { '-' } else { $canary.Value }

Write-Host "[AUDIT] config:" -ForegroundColor DarkGray
Write-Host ("  ask_channel_id={0} (source={1})" -f $askVal, $ask.Source) -ForegroundColor DarkGray
Write-Host ("  cal_earnings_channel_id={0} (source={1})" -f $calVal, $cal.Source) -ForegroundColor DarkGray
Write-Host ("  canary_channel_id={0} (source={1})" -f $canaryVal, $canary.Source) -ForegroundColor DarkGray

Push-Location -LiteralPath $root
try {
    & $py -u scripts\connected_audit_one_shot.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
