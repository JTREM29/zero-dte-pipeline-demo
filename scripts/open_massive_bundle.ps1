param(
    [Parameter(Mandatory = $true)][string]$Section
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$sectionNorm = $Section.Trim('/').Replace('\\','/')

$bundleRel = Join-Path 'tools\massive_docs\cache' (Join-Path ($sectionNorm.Replace('/','\')) '_bundle.md')
$bundlePath = Join-Path $repoRoot $bundleRel

if (-not (Test-Path -LiteralPath $bundlePath)) {
    throw "Bundle not found: $bundlePath (run Docs: Refresh Massive first)"
}

# Prefer VS Code if 'code' is available; otherwise fall back to default opener.
$codeCmd = Get-Command code -ErrorAction SilentlyContinue
if ($codeCmd) {
    & $codeCmd.Path -r $bundlePath
} else {
    Start-Process $bundlePath
}
