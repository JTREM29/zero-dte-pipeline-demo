param(
    [int]$Tail = 50
)

$ErrorActionPreference = 'Continue'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$stdout = Join-Path $repoRoot 'logs\futures_ingest_stdout.log'
$stderr = Join-Path $repoRoot 'logs\futures_ingest_stderr.log'

Write-Output "[TAIL] futures_ingest_stdout.log (last $Tail)"
if (Test-Path -LiteralPath $stdout) {
    Get-Content -LiteralPath $stdout -Tail $Tail
} else {
    Write-Output "missing: $stdout"
}

Write-Output ""
Write-Output "[TAIL] futures_ingest_stderr.log (last $Tail)"
if (Test-Path -LiteralPath $stderr) {
    Get-Content -LiteralPath $stderr -Tail $Tail
} else {
    Write-Output "missing: $stderr"
}
