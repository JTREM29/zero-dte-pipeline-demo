# Generate a premarket morning brief markdown and print a short status
param(
    [switch]$Open,
    [string]$Model = 'gpt-4o-mini',
    [switch]$NoNews,
    [int]$NewsMinutes = 5,
    [int]$FreshWithinSec = 180,
    [int]$StepTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..')
Set-Location $repoRoot

$python = if (Test-Path '.venv/Scripts/python.exe') { '.venv/Scripts/python.exe' } else { 'python' }

# Resolve output path by date
$tz = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$nowEt = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $tz)
$dayStamp = $nowEt.ToString('yyyyMMdd')
$out = Join-Path $repoRoot "logs/morning_brief_${dayStamp}.md"

# Helper: run a process with a hard timeout (kill if exceeded)
function Invoke-WithTimeout {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [int]$TimeoutSec = 600,
        [string]$Name = ''
    )
    $p = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -PassThru -NoNewWindow
    try {
        $ok = $p.WaitForExit($TimeoutSec * 1000)
        if (-not $ok) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
            throw "Step '$Name' exceeded timeout ${TimeoutSec}s and was terminated."
        }
        return $p.ExitCode
    }
    catch {
        try { if (!$p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } } catch {}
        throw
    }
}

# Ensure fresh news JSONL exists (IQFeed) unless explicitly disabled
if (-not $NoNews) {
    try {
        $logs = Join-Path $repoRoot 'logs'
        New-Item -ItemType Directory -Force -Path $logs | Out-Null
        $latest = Get-ChildItem -Path $logs -Filter ("news_watch_${dayStamp}*.jsonl") -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $needFresh = $true
        if ($latest) {
            $ageSec = [int]([DateTimeOffset]::Now.Subtract($latest.LastWriteTime).TotalSeconds)
            # If last capture is within ~3 minutes, consider it fresh
            if ($ageSec -lt $FreshWithinSec) { $needFresh = $false }
        }
        if ($needFresh) {
            Write-Host "[brief] capturing fresh IQFeed news for $NewsMinutes minutes..." -ForegroundColor Yellow
            $newsWatch = Join-Path $logs ("news_watch_${dayStamp}.jsonl")
            $nwPs = Join-Path $scriptDir 'start_news_watch.ps1'
            $pwshArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $nwPs, '-Minutes', [string]$NewsMinutes, '-Out', $newsWatch)
            Invoke-WithTimeout -FilePath 'powershell.exe' -ArgumentList $pwshArgs -TimeoutSec ($NewsMinutes * 60 + 60) -Name 'news_watch'
        }
    }
    catch { Write-Warning "news freshness step failed: $_" }
}

$cliArgs = @('morning_brief', '--model', $Model, '--out-md', $out)
if ($NoNews) { $cliArgs += '--no-include-news' }

# Run the brief generation with a timeout too (default 10 minutes)
$argList = @('-m', 'src.cli') + $cliArgs
try {
    $exit = Invoke-WithTimeout -FilePath $python -ArgumentList $argList -TimeoutSec $StepTimeoutSec -Name 'morning_brief'
    if ($exit -ne 0) { Write-Error "morning_brief failed (exit $exit)"; exit 1 }
}
catch { Write-Error $_; exit 1 }

Write-Host "Morning brief at $out" -ForegroundColor Green
if ($Open) {
    # Running on Windows PowerShell; open the file
    Start-Process $out
}
