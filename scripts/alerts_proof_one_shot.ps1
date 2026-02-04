param(
    [int]$Lines = 40
)

$ErrorActionPreference = 'SilentlyContinue'

Set-Location $PSScriptRoot
Set-Location ..

# Ensure local imports work (e.g. services.redis_env).
if (-not $env:PYTHONPATH) {
    $env:PYTHONPATH = '.'
}

$filters = 'TRIGGER|ENQUEUE|POP|DONE|POSTED|HEARTBEAT|SCHED|TICK|WORKER|DELIVERY|CONTEXT|AUTOPOST|OUTLOOK|EARNINGS|Enabled|WARN|FAIL|HTTPException|Forbidden|Traceback'

function Tail-Match {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Label,
        [int]$Tail = 2000
    )

    Write-Output ("--- {0} ---" -f $Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Output ("missing {0}" -f $Path)
        return
    }

    Get-Content -LiteralPath $Path -Tail $Tail |
        Select-String $filters |
        Select-Object -Last $Lines |
        ForEach-Object { $_.Line }
}

function Resolve-FirstExisting {
    param(
        [Parameter(Mandatory=$true)][string[]]$Candidates
    )

    foreach ($p in $Candidates) {
        if (Test-Path -LiteralPath $p) {
            return $p
        }
    }
    return $Candidates[0]
}

Write-Output "=== TNT One-Shot Proof (screenshot) ==="

try {
    & .\.venv\Scripts\python.exe -u scripts\redis_llen_once.py
} catch {
    Write-Output "redis_llen_once failed"
}

$sched1m = Resolve-FirstExisting -Candidates @(
    '.\logs\alerts_scheduler_1m.log',
    '.\logs\alerts_scheduler_v2_1m.log',
    '.\logs\alerts_scheduler_live.log',
    '.\logs\alerts_scheduler_v2_live.log'
)

$sched5m = Resolve-FirstExisting -Candidates @(
    '.\logs\alerts_scheduler_5m.log',
    '.\logs\alerts_scheduler_v2_5m.log',
    '.\logs\alerts_scheduler_live.log',
    '.\logs\alerts_scheduler_v2_live.log'
)

Tail-Match -Path $sched1m -Label 'scheduler 1m (TRIGGER/ENQUEUE)' -Tail 600
Tail-Match -Path $sched5m -Label 'scheduler 5m (TRIGGER/ENQUEUE)' -Tail 600
Tail-Match -Path .\logs\redis_worker_live.log -Label 'worker (POP/DONE/ENQUEUE_DISCORD)' -Tail 800
Tail-Match -Path .\logs\slash_live.log -Label 'slash bot (POSTED/HEARTBEAT/AUTOPOST)' -Tail 1400
Tail-Match -Path .\logs\earnings_scheduler_live.log -Label 'earnings scheduler (START/ok/err)' -Tail 400
Tail-Match -Path .\logs\context_writer_live.log -Label 'context writer (START/FAIL)' -Tail 200
