param(
    [string]$RepoRoot = "$env:USERPROFILE\OneDrive\Desktop\ZeroDTE-pipeline",
    [string]$PythonExe = "$env:USERPROFILE\OneDrive\Desktop\ZeroDTE-pipeline\.venv\Scripts\python.exe"
)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
$py = (Resolve-Path -LiteralPath $PythonExe).Path

Push-Location -LiteralPath $root
try {
    & $py -u scripts\futures_ops_proof_one_shot.py
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
