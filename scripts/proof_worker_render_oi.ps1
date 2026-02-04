param(
  [string]$Symbol = "SPY",
  [string]$WorkerBase = "http://192.168.1.145:8787",
  [string]$Out = ".\worker_SPY.png"
)

$Url = ($WorkerBase.TrimEnd("/") + "/v1/render/oi")
$Body = @{ symbol = $Symbol } | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri $Url -ContentType "application/json" -Body $Body -OutFile $Out

if (-not (Test-Path $Out)) { throw "FAIL: $Out was not created." }
$fi = Get-Item $Out
"OK: file exists -> {0} bytes" -f $fi.Length

# Verify PNG signature (first 8 bytes must be: 89 50 4E 47 0D 0A 1A 0A)
$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Out)) | Select-Object -First 8
$hex = ($bytes | ForEach-Object { $_.ToString("X2") }) -join " "
"Header bytes: $hex"

$expected = "89 50 4E 47 0D 0A 1A 0A"
if ($hex -ne $expected) { throw "FAIL: Not a valid PNG signature. Expected: $expected" }

"PASS: Valid PNG signature"
