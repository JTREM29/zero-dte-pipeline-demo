param(
  [string]$WorkerUrl = $env:TNT_WORKER_URL,
  [string]$Symbol = "SPY",
  [string]$OutFile = ".\tmp\worker_probe.png",
  [switch]$FailFast
)

$ErrorActionPreference = 'Stop'

if (-not $WorkerUrl) { throw "TNT_WORKER_URL is not set." }

# Normalize trailing slash
if ($WorkerUrl.EndsWith('/')) { $WorkerUrl = $WorkerUrl.TrimEnd('/') }

$health = Invoke-RestMethod "$WorkerUrl/healthz"
"healthz: " + ($health | ConvertTo-Json -Compress)

if ($FailFast) {
  $svc = "" + $health.service
  if ($svc -ne "tnt-worker-parity") {
    throw "worker service mismatch: expected=tnt-worker-parity got=$svc"
  }
}

# Deterministic payload probe (does not touch market-data).
$payload = @{
  symbol = $Symbol
  strikes = @(480.0, 485.0, 490.0, 495.0, 500.0)
  call_oi = @(12000, 18000, 35000, 42000, 78000)
  put_oi = @(9000, 15000, 24000, 38000, 66000)
  call_iv = @(22.1, 21.7, 21.4, 21.2, 20.9)
  put_iv = $null
  title = "META-PROBE - $Symbol"
  include_iv_overlay = $true
}

# Ensure out dir exists
$parent = Split-Path -Parent $OutFile
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$body = ($payload | ConvertTo-Json -Depth 6 -Compress)
Invoke-RestMethod -Method Post -Uri "$WorkerUrl/v1/render/oi_iv" -ContentType "application/json" -Body $body -OutFile $OutFile
"saved: $OutFile ($((Get-Item -LiteralPath $OutFile).Length) bytes)"

# Prefer repo venv python if present, else fall back to `py`.
$py = ".\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $py) {
  $pyCmd = $py
} else {
  $pyCmd = "py"
}

if ($FailFast) {
  & $pyCmd .\tmp\worker_meta_probe.py --file $OutFile --expect-layout v2
} else {
  & $pyCmd .\tmp\worker_meta_probe.py --file $OutFile
}
