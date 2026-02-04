param(
    [switch]$IncludeSupervisors,
    [switch]$NoLockCleanup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$repoRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$escapedRepo = [regex]::Escape($repoRoot)

function Stop-ByPattern {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)][string]$Regex
    )

    $procs = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $escapedRepo) -and ($_.CommandLine -match $Regex)
    })

    if (-not $procs -or $procs.Count -eq 0) {
        Write-Host ("[OPS] {0}: none" -f $Role) -ForegroundColor DarkGray
        return
    }

    foreach ($p in $procs) {
        $msg = ("[OPS] stopping {0} pid={1}" -f $Role, $p.ProcessId)
        if ($PSCmdlet.ShouldProcess($msg, 'Stop-Process')) {
            Write-Host $msg -ForegroundColor Yellow
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "[OPS] TNT stop-all" -ForegroundColor Cyan
Write-Host "[OPS] repo=$repoRoot" -ForegroundColor DarkGray

if ($IncludeSupervisors) {
    # Best-effort: stop any repo supervisors first so they don't auto-restart.
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and ($_.CommandLine -match $escapedRepo) -and ($_.Name -match '^(powershell|pwsh)') -and (
                ($_.CommandLine -like '*run_discord_supervised.ps1*') -or
                ($_.CommandLine -like '*run_clx_supervised.ps1*') -or
                ($_.CommandLine -like '*run_*supervised.ps1*')
            )
        } |
        ForEach-Object {
            Write-Host "[OPS] stopping supervisor pid=$($_.ProcessId)" -ForegroundColor Yellow
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

# Stop only the core processes that start_tnt_all_bg.ps1 launches.
Stop-ByPattern -Role 'FUTURES_INGEST' -Regex 'futures_ingest_databento\.py'
Stop-ByPattern -Role 'REDIS_WORKER'   -Regex '(-m\s+massive_service\.redis_worker)|(\bmassive_service\.redis_worker\b)'
Stop-ByPattern -Role 'NEWS_POLLER'    -Regex '(news_poller\.py)|(-m\s+scripts\.news_poller)|(\bscripts\.news_poller\b)'
Stop-ByPattern -Role 'SLASH_BOT'      -Regex '(-m\s+cli\.discord_bot)|(\bcli\.discord_bot\b)'
Stop-ByPattern -Role 'DELIVERY_BOT'   -Regex '(-m\s+delivery\.discord_bot)|(\bdelivery\.discord_bot\b)'

if (-not $NoLockCleanup) {
    try {
        $logDir = Join-Path $repoRoot 'logs'
        $lock1 = Join-Path $logDir 'tnt_futures_ingest.lock'
        $lock2 = Join-Path $logDir 'futures_ingest.pid'
        if (Test-Path -LiteralPath $lock1) { Remove-Item -LiteralPath $lock1 -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $lock2) { Remove-Item -LiteralPath $lock2 -Force -ErrorAction SilentlyContinue }
    } catch {
        # ignore
    }
}

Write-Host "[OK] stop-all executed." -ForegroundColor Green
