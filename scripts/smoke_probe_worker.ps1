param(
  [string]$WorkerUrl = "http://192.168.1.145:8787",
  [string]$OutFile = ".\\tmp\\worker_smoke.png"
)

$ErrorActionPreference = 'Stop'

if ($WorkerUrl.EndsWith('/')) { $WorkerUrl = $WorkerUrl.TrimEnd('/') }

$parent = Split-Path -Parent $OutFile
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

try {
  Invoke-RestMethod "$WorkerUrl/v1/render/smoke" -OutFile $OutFile
  "saved: $OutFile ($((Get-Item -LiteralPath $OutFile).Length) bytes)"

  if (-not (Test-Path -LiteralPath $OutFile)) { throw "smoke output missing" }
  $h = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $OutFile)) | Select-Object -First 8
  ($h | ForEach-Object { $_.ToString('X2') }) -join ' '
} catch {
  "ERROR: $($_.Exception.Message)"
  if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
    "ERROR_DETAILS:"
    $_.ErrorDetails.Message
  }
  if ($_.Exception.Response) {
    try {
      $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
      "BODY:"
      $sr.ReadToEnd()
    } catch {}
  }
  exit 1
}
