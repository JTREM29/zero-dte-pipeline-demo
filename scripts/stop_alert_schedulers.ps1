$ErrorActionPreference = 'SilentlyContinue'

$rx = 'run_alert_scheduler_5m\.py'

$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $rx)
}

if ($procs) {
    foreach ($p in $procs) {
        try {
            Write-Host ("Stopping PID=$($p.ProcessId) $($p.CommandLine)")
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
} else {
    Write-Host 'No scheduler procs found.'
}
