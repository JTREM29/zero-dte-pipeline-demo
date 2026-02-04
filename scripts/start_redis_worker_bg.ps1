param(
    [string]$PythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe",
    [string]$LogDir = "$PSScriptRoot\..\logs",
    [switch]$NoNewWindow
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$py = (Resolve-Path -LiteralPath $PythonExe).Path

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logDirAbs = (Resolve-Path -LiteralPath $LogDir).Path

# Ensure module resolution matches repo runners.
$env:PYTHONPATH = '.'

# Safe defaults (do not override if already set)
if (-not $env:TNT_REDIS_HOST) { $env:TNT_REDIS_HOST = '127.0.0.1' }
if (-not $env:TNT_REDIS_PORT) { $env:TNT_REDIS_PORT = '6379' }
if (-not $env:TNT_REDIS_DB) { $env:TNT_REDIS_DB = '0' }

$out = Join-Path $logDirAbs 'redis_worker_live.log'
$err = Join-Path $logDirAbs 'redis_worker_live.err.log'

$spArgs = @{
    FilePath               = $py
    ArgumentList           = @('-u','-m','massive_service.redis_worker')
    WorkingDirectory       = $repoRoot
    RedirectStandardOutput = $out
    RedirectStandardError  = $err
    PassThru               = $true
}
if ($NoNewWindow) {
    $spArgs['NoNewWindow'] = $true
} else {
    $spArgs['WindowStyle'] = 'Hidden'
}

Write-Output "[OPS] starting massive_service.redis_worker"
Write-Output "[OPS]   repo=$repoRoot"
Write-Output "[OPS]   out =$out"
Write-Output "[OPS]   err =$err"

$p = Start-Process @spArgs
Write-Output "[OPS] started pid=$($p.Id)"
