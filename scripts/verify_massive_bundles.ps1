Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$root = Join-Path $repoRoot 'tools\massive_docs\cache'

$paths = @(
    (Join-Path $root 'rest\options\_bundle.md'),
    (Join-Path $root 'rest\stocks\_bundle.md'),
    (Join-Path $root 'websocket\_bundle.md')
)

$missing = @()
foreach ($p in $paths) {
    if (-not (Test-Path -LiteralPath $p)) {
        $missing += $p
    }
}

if ($missing.Count -eq 0) {
    Write-Host 'OK=1 Massive docs bundles present' -ForegroundColor Green
    exit 0
}

Write-Host 'OK=0 Missing bundles:' -ForegroundColor Yellow
$missing | ForEach-Object { Write-Host (" - {0}" -f $_) }
exit 1
