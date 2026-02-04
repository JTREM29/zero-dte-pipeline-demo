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

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $logDirAbs "delivery_bot_$ts.out.log"
$err = Join-Path $logDirAbs "delivery_bot_$ts.err.log"

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

# Ensure setx-saved env vars are visible to this detached launcher.
Hydrate-EnvVarFromRegistry -Name 'TNT_NODE_ROLE'
Hydrate-EnvVarFromRegistry -Name 'TNT_WORKER_URL'
Hydrate-EnvVarFromRegistry -Name 'TNT_OI_WORKER_MODE'
Hydrate-EnvVarFromRegistry -Name 'TNT_OI_WORKER_PARITY_MODE'
Hydrate-EnvVarFromRegistry -Name 'TNT_WORKER_SANITIZE_ALWAYS'

# Log effective session env (avoid string concatenation pitfalls).
try {
    $w = [Environment]::GetEnvironmentVariable('TNT_WORKER_URL','Process')
    $m = [Environment]::GetEnvironmentVariable('TNT_OI_WORKER_MODE','Process')
    Write-Host ("[OPS] SESSION TNT_WORKER_URL={0}" -f ($w -as [string])) -ForegroundColor DarkGray
    Write-Host ("[OPS] SESSION TNT_OI_WORKER_MODE={0}" -f ($m -as [string])) -ForegroundColor DarkGray
} catch {
    # non-fatal
}

$spArgs = @{
    FilePath               = $py
    ArgumentList           = @('-u','-m','delivery.discord_bot')
    WorkingDirectory       = $repoRoot
    RedirectStandardOutput = $out
    RedirectStandardError  = $err
    PassThru               = $true
}
if ($NoNewWindow) {
    $spArgs['NoNewWindow'] = $true
}

Write-Host "[OPS] starting delivery.discord_bot" -ForegroundColor Cyan
Write-Host "[OPS]   repo=$repoRoot" -ForegroundColor DarkGray
Write-Host "[OPS]   out =$out" -ForegroundColor DarkGray
Write-Host "[OPS]   err =$err" -ForegroundColor DarkGray

$p = Start-Process @spArgs
Write-Host "[OPS] started pid=$($p.Id)" -ForegroundColor Green
