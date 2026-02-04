# Starts the market ingest loop in the background with environment variables loaded.
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\start_ingest_background.ps1

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$ingestScript = Join-Path $repoRoot "run_ingest.ps1"

if (-not (Test-Path $ingestScript)) {
    throw "Missing ingest script: $ingestScript"
}

Write-Host "[RUN] Launching market ingest in a separate PowerShell window..."
$proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoLogo", "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ingestScript `
    -WorkingDirectory $repoRoot `
    -WindowStyle Minimized `
    -PassThru
Write-Host "[OK] Ingest started (PowerShell PID=$($proc.Id)). Window is minimized; use 'Get-Process powershell' to monitor."
