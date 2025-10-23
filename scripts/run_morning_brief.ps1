# Generate a premarket morning brief markdown and print a short status
param(
    [switch]$Open,
    [string]$Model = 'gpt-4o-mini',
    [switch]$NoNews,
    [int]$NewsMinutes = 5,
    [int]$FreshWithinSec = 180,
    [int]$StepTimeoutSec = 600,
    [switch]$PostToDiscord,
    [string]$DiscordWebhookUrl,
    [string]$DiscordUsername = 'ZeroDTE-Morning'
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..')
Set-Location $repoRoot

# Resolve Python: prefer .venv312, then .venv, else fall back to PATH
if (Test-Path '.venv312/\Scripts\python.exe') {
    $python = '.venv312/\Scripts\python.exe'
}
elseif (Test-Path '.venv/\Scripts\python.exe') {
    $python = '.venv/\Scripts\python.exe'
}
else {
    $python = 'python'
}

# Resolve output path by date
$tz = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$nowEt = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $tz)
$dayStamp = $nowEt.ToString('yyyyMMdd')
$out = Join-Path $repoRoot "logs/morning_brief_${dayStamp}.md"

# Skip weekends and US market holidays (NYSE) automatically
function Get-EasterDate {
    param([int]$Year)
    # Anonymous Gregorian algorithm (Computus)
    $a = $Year % 19
    $b = [math]::Floor($Year / 100)
    $c = $Year % 100
    $d = [math]::Floor($b / 4)
    $e = $b % 4
    $f = [math]::Floor(($b + 8) / 25)
    $g = [math]::Floor(($b - $f + 1) / 3)
    $h = (19 * $a + $b - $d - $g + 15) % 30
    $i = [math]::Floor($c / 4)
    $k = $c % 4
    $l = (32 + 2 * $e + 2 * $i - $h - $k) % 7
    $m = [math]::Floor(($a + 11 * $h + 22 * $l) / 451)
    $month = [math]::Floor(($h + $l - 7 * $m + 114) / 31)
    $day = (($h + $l - 7 * $m + 114) % 31) + 1
    return Get-Date -Year $Year -Month $month -Day $day
}
function Get-NYSEHolidays {
    param([int]$Year)
    # Helper to compute observed date for fixed-date holidays
    function Observe([datetime]$dt) {
        switch ($dt.DayOfWeek) {
            'Saturday' { return $dt.AddDays(-1) }
            'Sunday' { return $dt.AddDays(1) }
            default { return $dt }
        }
    }
    # Nth weekday helpers
    function NthWeekday([int]$y, [int]$m, [System.DayOfWeek]$dow, [int]$n) {
        $first = Get-Date -Year $y -Month $m -Day 1
        $offset = ([int]$dow - [int]$first.DayOfWeek + 7) % 7
        return $first.AddDays($offset + 7 * ($n - 1))
    }
    function LastWeekday([int]$y, [int]$m, [System.DayOfWeek]$dow) {
        $firstNext = (Get-Date -Year $y -Month $m -Day 1).AddMonths(1)
        $last = $firstNext.AddDays(-1)
        $offset = ([int]$last.DayOfWeek - [int]$dow + 7) % 7
        return $last.AddDays(-$offset)
    }
    $list = New-Object 'System.Collections.Generic.HashSet[datetime]'
    # Fixed-date (with observation)
    [void]$list.Add((Observe (Get-Date -Year $Year -Month 1 -Day 1)))     # New Year's Day
    [void]$list.Add((Observe (Get-Date -Year $Year -Month 6 -Day 19)))    # Juneteenth
    [void]$list.Add((Observe (Get-Date -Year $Year -Month 7 -Day 4)))     # Independence Day
    [void]$list.Add((Observe (Get-Date -Year $Year -Month 12 -Day 25)))   # Christmas Day
    # Mondays
    [void]$list.Add((NthWeekday $Year 1  ([System.DayOfWeek]::Monday) 3)) # MLK Day (3rd Mon Jan)
    [void]$list.Add((NthWeekday $Year 2  ([System.DayOfWeek]::Monday) 3)) # Presidents Day (3rd Mon Feb)
    [void]$list.Add((LastWeekday $Year 5  ([System.DayOfWeek]::Monday)))  # Memorial Day (last Mon May)
    [void]$list.Add((NthWeekday $Year 9  ([System.DayOfWeek]::Monday) 1)) # Labor Day (1st Mon Sep)
    # Thanksgiving (4th Thu Nov)
    [void]$list.Add((NthWeekday $Year 11 ([System.DayOfWeek]::Thursday) 4))
    # Good Friday (2 days before Easter Sunday)
    $easter = Get-EasterDate -Year $Year
    [void]$list.Add($easter.AddDays(-2).Date)
    return $list
}
function Test-IsMarketHoliday {
    param([datetime]$DateEt)
    if ($DateEt.DayOfWeek -in @([System.DayOfWeek]::Saturday, [System.DayOfWeek]::Sunday)) { return $true }
    $h = Get-NYSEHolidays -Year $DateEt.Year
    return $h.Contains($DateEt.Date)
}

if (Test-IsMarketHoliday -DateEt $nowEt.Date) {
    Write-Host "[brief] Skipping morning brief on market holiday/weekend ($($nowEt.Date.ToString('yyyy-MM-dd')))" -ForegroundColor Yellow
    exit 0
}

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
if ($PostToDiscord -or $DiscordWebhookUrl -or $env:DISCORD_WEBHOOK_URL) {
    $cliArgs += '--post-to-discord'
    $hook = if ($DiscordWebhookUrl) { $DiscordWebhookUrl } elseif ($env:DISCORD_WEBHOOK_URL) { $env:DISCORD_WEBHOOK_URL } else { $null }
    if ($hook) { $cliArgs += @('--discord-webhook-url', $hook) }
    if ($DiscordUsername) { $cliArgs += @('--discord-username', $DiscordUsername) }
}
if ($NoNews) { $cliArgs += '--no-include-news' }

# Run the brief generation with a timeout too (default 10 minutes)
$argList = @('-m', 'src.cli') + $cliArgs
try {
    $exit = Invoke-WithTimeout -FilePath $python -ArgumentList $argList -TimeoutSec $StepTimeoutSec -Name 'morning_brief'
    # Some launch paths can yield a null ExitCode even on success; if so, treat as success when output exists
    if ($null -eq $exit) {
        if (Test-Path -LiteralPath $out) { $exit = 0 } else { $exit = 1 }
    }
    if ($exit -ne 0) { Write-Error "morning_brief failed (exit $exit)"; exit 1 }
}
catch { Write-Error $_; exit 1 }

Write-Host "Morning brief at $out" -ForegroundColor Green
if ($Open) {
    # Running on Windows PowerShell; open the file
    Start-Process $out
}
