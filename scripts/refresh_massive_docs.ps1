param(
    [string]$BaseUrl = $env:MASSIVE_DOCS_BASE_URL,
    [string[]]$Sections = @('rest','rest/options','rest/stocks','websocket'),
    [int]$MaxEndpoints = 200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
Set-Location $repoRoot

if (-not $BaseUrl -or -not $BaseUrl.Trim()) {
    $BaseUrl = 'https://massive.com'
}
$env:MASSIVE_DOCS_BASE_URL = $BaseUrl.Trim().TrimEnd('/')

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "missing $python (create venv first)"
}

$argsList = @(
    'tools/massive_docs/massive_docs.py',
    'refresh',
    '--sections'
) + $Sections + @(
    '--max-endpoints',
    [string]$MaxEndpoints
)

try {
    Write-Host "[docs] MASSIVE_DOCS_BASE_URL=$($env:MASSIVE_DOCS_BASE_URL)" -ForegroundColor DarkGray
    Write-Host "[docs] sections=$($Sections -join ',') max_endpoints=$MaxEndpoints" -ForegroundColor DarkGray

    & $python -u @argsList
    $code = [int]$LASTEXITCODE

    if ($code -eq 0) {
        Write-Output 'REFRESH_OK=1'
    } else {
        Write-Output ("REFRESH_OK=0 reason=exitcode_{0}" -f $code)
    }

    exit $code
} catch {
    Write-Output ("REFRESH_OK=0 reason={0}" -f ($_.Exception.Message -replace '\s+',' '))
    exit 2
}
