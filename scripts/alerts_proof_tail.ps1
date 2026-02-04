$ErrorActionPreference = 'SilentlyContinue'

Write-Host '--- redis_worker_live ---'
if (Test-Path .\logs\redis_worker_live.log) {
    Get-Content -Tail 1600 .\logs\redis_worker_live.log |
        Select-String "tnt:jobs|\[POP\]|\[CONSUME\]|\[WORKER\]|\[ALERTS\].*(ENQUEUE|TRIGGER|EVAL|JOB)" |
        Select-Object -Last 200 |
        ForEach-Object { $_.Line }
} else {
    Write-Host 'missing logs\redis_worker_live.log'
}

Write-Host '--- bot_live ---'
if (Test-Path .\logs\bot_live.log) {
    Get-Content -Tail 2000 .\logs\bot_live.log |
        Select-String "\[TNT\]\[ALERTS\]|tnt:jobs|\[JOBS\]|\[WORKER\]|\[POP\]|\[CONSUME\]|\[OK\].*posted|\[WARN\] publish failed|publish_id=" |
        Select-Object -Last 220 |
        ForEach-Object { $_.Line }
} else {
    Write-Host 'missing logs\bot_live.log'
}

Write-Host '--- slash_live ---'
if (Test-Path .\logs\slash_live.log) {
    Get-Content -Tail 2000 .\logs\slash_live.log |
        Select-String "\[TNT\]\[ALERTS\]\[DELIVERY\]\[(POP|BUNDLE|POSTED)\]|\[WARN\].*(publish|post)|Forbidden|HTTPException|\[OK\].*posted|publish_id=" |
        Select-Object -Last 220 |
        ForEach-Object { $_.Line }
} else {
    Write-Host 'missing logs\slash_live.log'
}

Write-Host '--- tnt:results tail (worker output) ---'
$python = Join-Path (Resolve-Path '.') '.venv\Scripts\python.exe'
if (Test-Path $python) {
    & $python -u scripts\redis_results_tail.py
} else {
    Write-Host 'missing .venv\Scripts\python.exe'
}
