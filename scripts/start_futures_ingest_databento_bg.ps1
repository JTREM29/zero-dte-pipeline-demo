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

function Hydrate-EnvVarFromRegistry {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )
    try {
        $existing = (Get-Item -Path ("Env:{0}" -f $Name) -ErrorAction SilentlyContinue).Value
        if ($existing) { return }
    } catch {}
    try {
        $v = [Environment]::GetEnvironmentVariable($Name, 'User')
        if (-not $v) { $v = [Environment]::GetEnvironmentVariable($Name, 'Machine') }
        if ($v) { Set-Item -Path ("Env:{0}" -f $Name) -Value $v }
    } catch {
        # non-fatal
    }
}

Hydrate-EnvVarFromRegistry -Name 'TNT_NODE_ROLE'

$out = Join-Path $logDirAbs 'futures_ingest_stdout.log'
$err = Join-Path $logDirAbs 'futures_ingest_stderr.log'

$scriptPath = Join-Path $repoRoot 'scripts\futures_ingest_databento.py'
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Missing script: $scriptPath"
}

$spArgs = @{
    FilePath               = $py
    ArgumentList           = @('-u', $scriptPath)
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

Write-Output "[OPS] starting futures_ingest_databento"
Write-Output "[OPS]   repo=$repoRoot"
Write-Output "[OPS]   out =$out"
Write-Output "[OPS]   err =$err"

$p = Start-Process @spArgs

# Best-effort PID breadcrumbs for ops.
try {
    Set-Content -LiteralPath (Join-Path $logDirAbs 'futures_ingest.pid') -Value $p.Id -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $logDirAbs 'tnt_futures_ingest.lock') -Value "pid=$($p.Id)" -Encoding ASCII
} catch {
    # non-fatal
}

Write-Output "[OPS] started pid=$($p.Id)"