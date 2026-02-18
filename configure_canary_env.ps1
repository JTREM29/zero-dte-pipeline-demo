# Writes/updates .env.local for a one-channel canary run.
# - Prompts for DISCORD_BOT_TOKEN with hidden input.
# - Sets DISCORD_CANARY_CHANNEL_ID.
# - (Optional) also sets TNT_OPS_CHANNEL_ID to the same value, but the v1 beta launcher
#   already routes ops/heartbeat to the canary channel at runtime.
# Usage:
#   .\configure_canary_env.ps1
#   .\configure_canary_env.ps1 -CanaryChannelId 1451014818827079750
# Optional (crypto watchlist command):
#   .\configure_canary_env.ps1 -SetCryptoWatchlistDefaults
#   .\configure_canary_env.ps1 -SetCryptoWatchlistDefaults -CryptoWatchlist "BTC,ETH,XRP,DOGE,LTC" -CryptoWatchlistTtlSec 300
#
param(
    [string]$CanaryChannelId = '1451014818827079750',
    [switch]$SetOpsSameAsCanary = $false,
    [switch]$ResetToken = $false,
    [ValidateSet('', 'CANARY', 'PROD_CONSERVATIVE', 'PROD_BALANCED')][string]$EdgeProfilePreset = '',
    [string]$EdgeProfileJson = '',
    [switch]$ClearEdgeProfile = $false,
    [switch]$SetCryptoWatchlistDefaults = $false,
    [string]$CryptoWatchlist = '',
    [int]$CryptoWatchlistTtlSec = 0
)

$ErrorActionPreference = 'Stop'

if ($ResetToken) {
    $confirm = Read-Host -Prompt 'ResetToken requested. Type RESET to confirm (anything else cancels)'
    if ($confirm -ne 'RESET') {
        Write-Output 'ResetToken cancelled; existing token will be reused if present.'
        $ResetToken = $false
    }
}

function Read-SecretToPlainText {
    param([Parameter(Mandatory=$true)][string]$Prompt)
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    if ($null -eq $sec) { return $null }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Normalize-EnvValue {
    param([string]$Value)
    if ($null -eq $Value) { return '' }
    $v = $Value.Trim()
    if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
        if ($v.Length -ge 2) { $v = $v.Substring(1, $v.Length - 2) }
    }
    return $v
}

$canary = Normalize-EnvValue $CanaryChannelId
if ([string]::IsNullOrWhiteSpace($canary)) {
    throw 'CanaryChannelId cannot be blank.'
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $scriptRoot '.env.local'

$lines = @()
if (Test-Path -LiteralPath $envPath) {
    $lines = Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue
}

function Get-EnvValueFromLines {
    param(
        [AllowEmptyCollection()][AllowNull()][string[]]$Lines,
        [Parameter(Mandatory=$true)][string]$Key
    )
    foreach ($line in $Lines) {
        $t = $line.Trim()
        if ($t.Length -eq 0) { continue }
        if ($t.StartsWith('#')) { continue }
        if ($t -match ('^' + [regex]::Escape($Key) + '\s*=') ) {
            $parts = $t -split '=', 2
            if ($parts.Count -lt 2) { return '' }
            return (Normalize-EnvValue $parts[1])
        }
    }
    return ''
}

$existingToken = Get-EnvValueFromLines -Lines @($lines) -Key 'DISCORD_BOT_TOKEN'
$token = ''

if (-not $ResetToken -and -not [string]::IsNullOrWhiteSpace($existingToken)) {
    $answer = Read-Host -Prompt 'Existing DISCORD_BOT_TOKEN found in .env.local. Reuse it? [Y/n]'
    if ([string]::IsNullOrWhiteSpace($answer) -or $answer.Trim().ToLower().StartsWith('y')) {
        $token = $existingToken
    }
}

if ([string]::IsNullOrWhiteSpace($token)) {
    $token = Read-SecretToPlainText -Prompt 'DISCORD_BOT_TOKEN (bot token; input hidden)'
    $token = Normalize-EnvValue $token
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'DISCORD_BOT_TOKEN cannot be blank.'
    }
}

$keyToIndex = @{}
for ($i = 0; $i -lt $lines.Count; $i++) {
    $t = $lines[$i].Trim()
    if ($t.Length -eq 0) { continue }
    if ($t.StartsWith('#')) { continue }
    $m = [regex]::Match($t, '^(?<k>[A-Za-z_][A-Za-z0-9_]*)\s*=')
    if (-not $m.Success) { continue }
    $k = $m.Groups['k'].Value
    if (-not $keyToIndex.ContainsKey($k)) {
        $keyToIndex[$k] = $i
    }
}

function Upsert-Line {
    param(
        [Parameter(Mandatory=$true)][string]$Key,
        [Parameter(Mandatory=$true)][string]$Value
    )
    if ($keyToIndex.ContainsKey($Key)) {
        $idx = [int]$keyToIndex[$Key]
        $script:lines[$idx] = "$Key=$Value"
    }
    else {
        $keyToIndex[$Key] = $script:lines.Count
        $script:lines += "$Key=$Value"
    }
}

function Build-EdgeProfileJson {
    param(
        [ValidateSet('', 'CANARY', 'PROD_CONSERVATIVE', 'PROD_BALANCED')][string]$Preset,
        [string]$RawJson
    )

    $raw = Normalize-EnvValue $RawJson
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        try {
            $obj = $raw | ConvertFrom-Json -ErrorAction Stop
            return ($obj | ConvertTo-Json -Compress -Depth 8)
        }
        catch {
            throw "EdgeProfileJson must be valid JSON. Error: $($_.Exception.Message)"
        }
    }

    $p = (Normalize-EnvValue $Preset).ToUpperInvariant()
    if ([string]::IsNullOrWhiteSpace($p)) {
        return ''
    }

    if ($p -eq 'CANARY') {
        $obj = @{
            risk_budget = 'LOW'
            hold_time = 'INTRADAY'
            preferred_setups = @('pivot reclaim', 'range break with acceptance')
            avoid = @('late entries', 'chop at pivot', 'into high-impact event window')
            notes = 'Canary: be conservative; prioritize clean acceptance/rejection.'
        }
        return ($obj | ConvertTo-Json -Compress -Depth 8)
    }

    if ($p -eq 'PROD_CONSERVATIVE') {
        $obj = @{
            risk_budget = 'LOW'
            hold_time = 'INTRADAY'
            preferred_setups = @('trend day follow-through', 'opening range break with confirmation')
            avoid = @('mean reversion fades', 'late-day initiation')
            notes = 'Conservative: reduce size into volatility and avoid marginal edges.'
        }
        return ($obj | ConvertTo-Json -Compress -Depth 8)
    }

    if ($p -eq 'PROD_BALANCED') {
        $obj = @{
            risk_budget = 'MEDIUM'
            hold_time = 'INTRADAY'
            preferred_setups = @('pivot reclaim', 'breakout/breakdown with retest', 'mean reversion from extremes')
            avoid = @('chop at pivot', 'trading stale data')
            notes = 'Balanced: only press when TVDS is GREEN/YELLOW and structure is clean.'
        }
        return ($obj | ConvertTo-Json -Compress -Depth 8)
    }

    throw "Unknown EdgeProfilePreset '$Preset'."
}

if ($lines.Count -eq 0) {
    $lines = @(
        '# Local overrides (git-ignored)',
        '# One-channel canary: posts + ops + heartbeat can share the canary channel',
        ''
    )
}

Upsert-Line -Key 'DISCORD_CANARY_CHANNEL_ID' -Value $canary
Upsert-Line -Key 'DISCORD_BOT_TOKEN' -Value $token

# Canary defaults (safe to overwrite each run)
Upsert-Line -Key 'TNT_LLM_MODEL' -Value 'gpt-5.2'
Upsert-Line -Key 'TNT_RATE_USER_TEXT_MIN_SEC' -Value '0'

# User-requested: disable IQFeed completely
Upsert-Line -Key 'IQFEED_ENABLED' -Value '0'

if ($ClearEdgeProfile) {
    Upsert-Line -Key 'TNT_EDGE_PROFILE_JSON' -Value ''
}
else {
    $edgeJson = Build-EdgeProfileJson -Preset $EdgeProfilePreset -RawJson $EdgeProfileJson
    if (-not [string]::IsNullOrWhiteSpace($edgeJson)) {
        Upsert-Line -Key 'TNT_EDGE_PROFILE_JSON' -Value $edgeJson
    }
}

if ($SetOpsSameAsCanary) {
    Upsert-Line -Key 'TNT_OPS_CHANNEL_ID' -Value $canary
}

if ($SetCryptoWatchlistDefaults) {
    $wl = Normalize-EnvValue $CryptoWatchlist
    if ([string]::IsNullOrWhiteSpace($wl)) {
        $wl = 'BTC,ETH,XRP,DOGE,LTC'
    }
    $ttl = [int]$CryptoWatchlistTtlSec
    if ($ttl -le 0) { $ttl = 300 }
    Upsert-Line -Key 'TNT_CRYPTO_WATCHLIST' -Value $wl
    Upsert-Line -Key 'TNT_TTL_CRYPTO_WATCHLIST_SEC' -Value "$ttl"
}

Set-Content -LiteralPath $envPath -Value $lines -Encoding utf8

Write-Output "Wrote .env.local"
Write-Output "  canary=$canary"
Write-Output "  token_set=True"
Write-Output "  ops_same_as_canary=$SetOpsSameAsCanary"

$edgeVal = Get-EnvValueFromLines -Lines @($lines) -Key 'TNT_EDGE_PROFILE_JSON'
if ([string]::IsNullOrWhiteSpace($edgeVal)) {
    Write-Output "  edge_profile=not_set"
}
else {
    Write-Output "  edge_profile=set"
}
