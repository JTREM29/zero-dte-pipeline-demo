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

# Ensure setx-saved env vars are visible to this detached launcher.
Hydrate-EnvVarFromRegistry -Name 'TNT_NODE_ROLE'
Hydrate-EnvVarFromRegistry -Name 'TNT_WORKER_URL'
Hydrate-EnvVarFromRegistry -Name 'TNT_OI_WORKER_MODE'
Hydrate-EnvVarFromRegistry -Name 'TNT_OI_WORKER_PARITY_MODE'
Hydrate-EnvVarFromRegistry -Name 'TNT_WORKER_SANITIZE_ALWAYS'
Hydrate-EnvVarFromRegistry -Name 'TNT_OI_WORKER_PARITY_PROBE_LOG'

# Log effective session env (avoid string concatenation pitfalls).
try {
    $w = [Environment]::GetEnvironmentVariable('TNT_WORKER_URL','Process')
    $m = [Environment]::GetEnvironmentVariable('TNT_OI_WORKER_MODE','Process')
    Write-Output ("[OPS] SESSION TNT_WORKER_URL={0}" -f ($w -as [string]))
    Write-Output ("[OPS] SESSION TNT_OI_WORKER_MODE={0}" -f ($m -as [string]))
} catch {
    # non-fatal
}

# The bot has its own file-based singleton lock, but on some Windows/OneDrive
# setups it can fail spuriously (interpreted as "another instance"), causing
# immediate exits. This script already enforces a single running PID by stopping
# existing instances and de-duping after start, so we disable the internal lock.
if (-not $env:TNT_ALLOW_MULTIPLE_BOTS) { $env:TNT_ALLOW_MULTIPLE_BOTS = '1' }

# Prefer repo-managed env files (.env/.env.local) and the caller's shell.
# Avoid forcing feature gates on in this launcher; it can make startup brittle
# when the shell lacks the full runtime env.

$out = Join-Path $logDirAbs 'slash_live.log'
$err = Join-Path $logDirAbs 'slash_live.err.log'

function Get-SlashBotProcs {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match 'cli\.discord_bot') } |
        Sort-Object CreationDate
}

function Stop-SlashBotProcs {
    param([int]$WaitSeconds = 5)
    $procs = @(Get-SlashBotProcs)
    if (-not $procs -or $procs.Count -eq 0) {
        return
    }
    Write-Output ("[OPS] stopping {0} existing cli.discord_bot process(es)..." -f $procs.Count)
    foreach ($p in $procs) {
        try {
            Write-Output ("[OPS]   stopping pid={0} started={1}" -f $p.ProcessId, $p.CreationDate)
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        $still = @(Get-SlashBotProcs)
        if (-not $still -or $still.Count -eq 0) { break }
    }
}

function Rotate-IfExists {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        try {
            $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
            $dir = Split-Path -Parent $Path
            $leaf = Split-Path -Leaf $Path
            $dst = Join-Path $dir ("{0}.prev_{1}" -f $leaf, $ts)
            Move-Item -LiteralPath $Path -Destination $dst -Force
        } catch {}
    }
}

$spArgs = @{
    FilePath               = $py
    ArgumentList           = @('-u','-m','cli.discord_bot')
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

Write-Output "[OPS] starting cli.discord_bot"
Write-Output "[OPS]   repo=$repoRoot"
Write-Output "[OPS]   out =$out"
Write-Output "[OPS]   err =$err"

# Avoid duplicate instances (and log clobbering) by stopping any existing bot first.
Stop-SlashBotProcs -WaitSeconds 6

# Rotate logs so this run has a clean, single-process trail.
Rotate-IfExists -Path $out
Rotate-IfExists -Path $err

$p = Start-Process @spArgs
Write-Output "[OPS] started pid=$($p.Id)"

# If it exits immediately, retry with singleton lock override (best-effort).
Start-Sleep -Seconds 2
$alive = $false
try {
    $null = Get-Process -Id $p.Id -ErrorAction Stop
    $alive = $true
} catch {
    $alive = $false
}

if (-not $alive) {
    Write-Output "[OPS][WARN] cli.discord_bot exited quickly; retrying with TNT_ALLOW_MULTIPLE_BOTS=1 (start script still enforces single PID)."
    $env:TNT_ALLOW_MULTIPLE_BOTS = '1'
    $p = Start-Process @spArgs
    Write-Output "[OPS] retry started pid=$($p.Id)"
    Start-Sleep -Seconds 2
}

# If something else raced and started another instance, keep the newest and stop the others.
$running = @(Get-SlashBotProcs)
if ($running -and $running.Count -gt 1) {
    # IMPORTANT: cli.discord_bot may legitimately run as a parent/child pair on Windows.
    # Treat that as ONE instance. Only stop processes when multiple independent roots exist.
    $pids = @($running | ForEach-Object { [int]$_.ProcessId })
    $pidSet = @{}
    foreach ($procPid in $pids) { $pidSet[$procPid] = $true }
    $roots = @($running | Where-Object { -not $pidSet.ContainsKey([int]$_.ParentProcessId) })

    try {
        Write-Output "[OPS] instances snapshot (pid/ppid/started):"
        $running | ForEach-Object { Write-Output ("[OPS]   pid={0} ppid={1} started={2}" -f $_.ProcessId, $_.ParentProcessId, $_.CreationDate) }
        Write-Output ("[OPS] root count={0}" -f (($roots | Measure-Object).Count))
    } catch {}

    if (($roots | Measure-Object).Count -gt 1) {
        $keepRoot = $roots | Sort-Object CreationDate, ProcessId -Descending | Select-Object -First 1
        $keepRootPid = [int]$keepRoot.ProcessId

        # Keep the chosen root and any direct children within the matched set.
        $keepPids = @{}
        $keepPids[$keepRootPid] = $true
        $running | Where-Object { [int]$_.ParentProcessId -eq $keepRootPid } | ForEach-Object { $keepPids[[int]$_.ProcessId] = $true }

        $kill = @($running | Where-Object { -not $keepPids.ContainsKey([int]$_.ProcessId) })
        $killCount = ($kill | Measure-Object).Count
        Write-Output ("[OPS] detected multiple bot roots; keeping root pid={0}, stopping {1} other process(es)..." -f $keepRootPid, $killCount)
        foreach ($k in $kill) {
            try { Stop-Process -Id $k.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
}