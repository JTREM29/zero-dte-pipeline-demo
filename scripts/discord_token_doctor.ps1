param(
    [string]$RepoRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Sha256Fp10([string]$Text) {
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
        $hex = -join ($hash | ForEach-Object { $_.ToString('x2') })
        return $hex.Substring(0, 10)
    } catch {
        return "(err)"
    }
}

function ConvertTo-NormalizedEnvValue([string]$Value) {
    if ($null -eq $Value) { return "" }
    $v = $Value.Trim()
    if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
        $v = $v.Substring(1, $v.Length - 2)
    }
    return $v.Trim()
}

function Normalize-EnvValue([string]$Value) {
    # Back-compat wrapper.
    return ConvertTo-NormalizedEnvValue $Value
}

function Get-TokenCandidatesFromFile([string]$Path, [string[]]$Keys) {
    $out = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $out }

    $lines = Get-Content -LiteralPath $Path -ErrorAction Stop
    foreach ($k in $Keys) {
        foreach ($lineRaw in $lines) {
            $work = ([string]$lineRaw).Trim()
            if (-not $work) { continue }
            if ($work.StartsWith('#')) { continue }

            # Support export/set/setx variants and KEY=VALUE
            $low = $work.ToLowerInvariant()
            if ($low.StartsWith('export ')) { $work = $work.Substring(7).TrimStart() }
            $low = $work.ToLowerInvariant()

            if ($low.StartsWith('setx ')) {
                $parts = $work.Split(' ', 3, [System.StringSplitOptions]::RemoveEmptyEntries)
                if ($parts.Length -ge 3 -and $parts[1] -eq $k) {
                    $val = ConvertTo-NormalizedEnvValue $parts[2]
                    if ($val) {
                        $out += [PSCustomObject]@{ source = "file:$([IO.Path]::GetFileName($Path)):$k"; len = $val.Length; fp = (Get-Sha256Fp10 $val); whitespace = ($val -match '\\s') }
                    }
                }
                continue
            }

            if ($low.StartsWith('set ')) {
                $work = $work.Substring(4).TrimStart()
            }

            if ($work -notmatch '=') { continue }
            $kv = $work.Split('=', 2)
            $key = $kv[0].Trim()
            if ($key -ne $k) { continue }

            $valRaw = $kv[1].Trim()
            # Strip inline comments for unquoted values: KEY=VALUE # comment
            if ($valRaw -and -not ($valRaw.StartsWith('"') -or $valRaw.StartsWith("'"))) {
                $hashIdx = $valRaw.IndexOf('#')
                if ($hashIdx -ge 0) {
                    $valRaw = $valRaw.Substring(0, $hashIdx).TrimEnd()
                }
            }

            $val = ConvertTo-NormalizedEnvValue $valRaw
            if ($val) {
                $out += [PSCustomObject]@{ source = "file:$([IO.Path]::GetFileName($Path)):$k"; len = $val.Length; fp = (Get-Sha256Fp10 $val); whitespace = ($val -match '\\s') }
            }
        }
    }

    return $out
}

function Get-TokenCandidatesFromEnv([string[]]$Keys) {
    $out = @()
    foreach ($k in $Keys) {
        $raw = [Environment]::GetEnvironmentVariable($k)
        if ($raw) {
            $val = ConvertTo-NormalizedEnvValue $raw
            if ($val) {
                $out += [PSCustomObject]@{ source = "env:$k"; len = $val.Length; fp = (Get-Sha256Fp10 $val); whitespace = ($val -match '\\s') }
            }
        }
    }
    return $out
}

$keys = @('DISCORD_BOT_TOKEN', 'DISCORD_TOKEN', 'TNT_DISCORD_TOKEN')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

$envLocal = Join-Path $RepoRoot '.env.local'
$envPlain = Join-Path $RepoRoot '.env'

Write-Host "[token-doctor] repo=$RepoRoot"
Write-Host "[token-doctor] env_local_exists=$([bool](Test-Path -LiteralPath $envLocal)) env_exists=$([bool](Test-Path -LiteralPath $envPlain))"

$cands = @()
$cands += Get-TokenCandidatesFromFile -Path $envLocal -Keys $keys
$cands += Get-TokenCandidatesFromFile -Path $envPlain -Keys $keys
$cands += Get-TokenCandidatesFromEnv -Keys $keys

# Deduplicate by fp
$dedup = @{}
foreach ($c in $cands) {
    if (-not $dedup.ContainsKey($c.fp)) {
        $dedup[$c.fp] = $c
    }
}

$cands2 = @($dedup.Values | Sort-Object source)

if (-not $cands2 -or $cands2.Count -eq 0) {
    Write-Host "[token-doctor][FATAL] No token candidates found in .env.local/.env or env vars." -ForegroundColor Red
    exit 2
}

Write-Host "[token-doctor] token candidates (safe):"
$cands2 | ForEach-Object {
    $ws = if ($_.whitespace) { 'YES' } else { 'no' }
    Write-Host ("  - {0} len={1} fp={2} whitespace={3}" -f $_.source, $_.len, $_.fp, $ws)
}

# Emulate the bot's precedence: .env.local then .env then env vars.
$selected = $null
foreach ($pref in @('file:.env.local:', 'file:.env:', 'env:')) {
    $selected = $cands2 | Where-Object { $_.source.StartsWith($pref) } | Select-Object -First 1
    if ($selected) { break }
}

if ($selected) {
    Write-Host "[token-doctor] selected_by_precedence: $($selected.source) len=$($selected.len) fp=$($selected.fp)"
}

if ($cands2.Count -gt 1) {
    Write-Host "[token-doctor][WARN] Multiple token candidates exist. Prefer ONLY one key in .env.local: DISCORD_BOT_TOKEN." -ForegroundColor Yellow
}

if ($selected -and ($selected.whitespace -eq $true)) {
    Write-Host "[token-doctor][FATAL] Selected token contains whitespace/newlines. Remove quotes/whitespace and re-paste." -ForegroundColor Red
    exit 3
}

exit 0
