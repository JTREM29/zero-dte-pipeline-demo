param(
  [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe",
  [string]$Host = "0.0.0.0",
  [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'

Write-Host "Starting TNT parity worker (checked) on ${Host}:${Port}..."

# Ensure we run from repo root so imports like delivery.* work.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$env:PYTHONPATH = "."

# Surface build id in /healthz for stale-deploy detection.
try {
  $env:TNT_BUILD = (git rev-parse --short HEAD)
} catch {
  # non-fatal
}

$py = $PythonExe
try { $py = (Resolve-Path -LiteralPath $PythonExe).Path } catch {}

Write-Host "Python: $py"
Write-Host "Repo:   $repo"

# Prove which module file will be used before starting uvicorn.
& $py -c "import worker.worker_api_parity as m; import sys; print('worker_api_parity:', m.__file__); print('executable:', sys.executable)"

& $py -m uvicorn worker.worker_api_parity:app --app-dir $repo --host $Host --port $Port
