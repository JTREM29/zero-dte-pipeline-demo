# NOTE: No-op change to refresh PowerShell diagnostics (PS 5.1 compatible).
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Repo root = parent of scripts/
$repo = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $repo

$logDir = Join-Path $repo 'logs\massive_contracts'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile = Join-Path $logDir ("refresh_massive_contracts_{0}.log" -f $ts)

$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    throw "Python venv not found at $py"
}

"=== refresh massive contracts ===" | Out-File -FilePath $logFile -Encoding utf8
"ts=$ts repo=$repo" | Out-File -FilePath $logFile -Append -Encoding utf8

function Get-GitCommitShort {
    try {
        $h = (& git rev-parse --short HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $h) { return ($h | Select-Object -First 1).Trim() }
    } catch { }
    return ''
}

function Get-LastJsonObject {
    param([string[]]$Lines)
    if (-not $Lines) { return $null }
    for ($i = $Lines.Length - 1; $i -ge 0; $i--) {
        $raw = $Lines[$i]
        if ($null -eq $raw) { $raw = '' }
        $line = ($raw).Trim()
        if (-not $line.StartsWith('{')) { continue }
        try { return ($line | ConvertFrom-Json) } catch { }
    }
    return $null
}

function Assert-HasProps {
    param(
        [object]$Obj,
        [string[]]$Props,
        [string]$Name
    )
    if ($null -eq $Obj) { throw "$Name summary JSON missing" }
    foreach ($p in $Props) {
        if (-not ($Obj.PSObject.Properties.Name -contains $p)) {
            throw "$Name summary missing required field: $p"
        }
    }
}

function Format-Elapsed {
    param([TimeSpan]$Elapsed)
    return $Elapsed.ToString('hh\:mm\:ss')
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Cmd
    )
    "--- step=$Name ---" | Out-File -FilePath $logFile -Append -Encoding utf8
    "--- step=$Name ---" | Write-Host
    $out = & $Cmd 2>&1
    $code = $LASTEXITCODE
    if ($out) { $out | Out-File -FilePath $logFile -Append -Encoding utf8 }
    if ($code -ne 0) {
        "step_failed=$Name exit_code=$code" | Out-File -FilePath $logFile -Append -Encoding utf8
        "step_failed=$Name exit_code=$code" | Write-Host
        exit $code
    }
    return ,$out
}

$gitHash = Get-GitCommitShort
$docsBaseEnv = ''
if ($env:MASSIVE_DOCS_BASE_URL) { $docsBaseEnv = [string]$env:MASSIVE_DOCS_BASE_URL }
$docsBaseEnv = $docsBaseEnv.Trim()
if (-not $docsBaseEnv) { $docsBaseEnv = 'https://massive.com' }

"git_commit_short=$gitHash" | Out-File -FilePath $logFile -Append -Encoding utf8
"docs_base_url_env=$docsBaseEnv" | Out-File -FilePath $logFile -Append -Encoding utf8

# Strict mode: fail if registry missing or required endpoints absent.
$env:MASSIVE_CONTRACT_TESTS_STRICT = '1'

$swTotal = [Diagnostics.Stopwatch]::StartNew()

$docsOut = Invoke-Step 'docs_sync' { & $py -u scripts\massive_docs_sync.py }
$docsJson = Get-LastJsonObject -Lines $docsOut
Assert-HasProps -Obj $docsJson -Props @('ok','base_url','indexes','pages_fetched','pages_written','skipped','elapsed_ms') -Name 'docs_sync'
if (-not [bool]$docsJson.ok) { throw "docs_sync ok!=true" }
if (-not $docsJson.base_url) { throw "docs_sync base_url missing" }
if (($docsJson.indexes | Measure-Object).Count -le 0) { throw "docs_sync indexes empty" }
if ([int]$docsJson.pages_written -le 0) { throw "docs_sync pages_written=0" }

"docs_base_url_resolved=$($docsJson.base_url)" | Out-File -FilePath $logFile -Append -Encoding utf8
"docs indexes=$($docsJson.indexes -join ',') pages_fetched=$($docsJson.pages_fetched) pages_written=$($docsJson.pages_written) skipped=$($docsJson.skipped) elapsed_ms=$($docsJson.elapsed_ms)" | Out-File -FilePath $logFile -Append -Encoding utf8
"docs ok=true base=$($docsJson.base_url) pages=$($docsJson.pages_written) skipped=$($docsJson.skipped) elapsed_ms=$($docsJson.elapsed_ms)" | Write-Host

$regOut = Invoke-Step 'registry_build' { & $py -u scripts\massive_registry_build.py }
$regJson = Get-LastJsonObject -Lines $regOut
Assert-HasProps -Obj $regJson -Props @('ok','md_files_scanned','entries_written','missing_method_or_path','registry_path','elapsed_ms') -Name 'registry_build'
if (-not [bool]$regJson.ok) { throw "registry_build ok!=true" }
if ([int]$regJson.md_files_scanned -le 0) { throw "registry_build md_files_scanned=0" }
if ([int]$regJson.entries_written -le 0) { throw "registry_build entries_written=0" }
if (-not $regJson.registry_path) { throw "registry_build registry_path missing" }

"registry files_scanned=$($regJson.md_files_scanned) entries_written=$($regJson.entries_written) missing_method_or_path=$($regJson.missing_method_or_path) registry_path=$($regJson.registry_path) elapsed_ms=$($regJson.elapsed_ms)" | Out-File -FilePath $logFile -Append -Encoding utf8
"registry ok=true files=$($regJson.md_files_scanned) entries=$($regJson.entries_written) missing=$($regJson.missing_method_or_path) elapsed_ms=$($regJson.elapsed_ms)" | Write-Host

Invoke-Step 'contract_tests' { & $py -u -m pytest -q tests\test_massive_contract_registry.py } | Out-Null

$swTotal.Stop()
$verdict = "OK docs=$($docsJson.pages_written) registry=$($regJson.entries_written) tests=pass base=$($docsJson.base_url) elapsed=$(Format-Elapsed -Elapsed $swTotal.Elapsed)"
$verdict | Out-File -FilePath $logFile -Append -Encoding utf8
$verdict | Write-Host
exit 0
