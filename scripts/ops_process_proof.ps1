param(
    [switch]$GrepProofLogs,
    [int]$Tail = 2500,
    [Alias('StrictConservative')][switch]$Strict,
    [switch]$StrictSingleton,
    [switch]$StrictTonight,
    [switch]$IncludeLanAsExternal,
    [string[]]$OnlyRoles
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'SilentlyContinue'

function Test-IsPrivateOrLoopback([string]$addr) {
    if (-not $addr) { return $true }
    if ($addr -in @('0.0.0.0', '127.0.0.1', '::1')) { return $true }

    $ip = $null
    # If we can't parse the address (rare/weird strings), do NOT count it as external.
    # This keeps “External” conservative (prevents LAN/home network oddities from inflating it).
    if (-not [System.Net.IPAddress]::TryParse($addr, [ref]$ip)) {
        return $true
    }

    if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        $b = $ip.GetAddressBytes()

        # loopback 127.0.0.0/8
        if ($b[0] -eq 127) { return $true }
        # link-local 169.254.0.0/16
        if ($b[0] -eq 169 -and $b[1] -eq 254) { return $true }
        # RFC1918 10.0.0.0/8
        if ($b[0] -eq 10) { return $true }
        # RFC1918 172.16.0.0/12
        if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return $true }
        # RFC1918 192.168.0.0/16
        if ($b[0] -eq 192 -and $b[1] -eq 168) { return $true }

        # CGNAT 100.64.0.0/10 (treat as private-ish by default)
        if ($b[0] -eq 100 -and $b[1] -ge 64 -and $b[1] -le 127) { return $true }

        return $false
    }

    if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
        if ([System.Net.IPAddress]::IsLoopback($ip)) { return $true }
        if ($ip.IsIPv6LinkLocal) { return $true }
        if ($ip.IsIPv6SiteLocal) { return $true }

        # Unique local addresses fc00::/7
        $b = $ip.GetAddressBytes()
        if (($b[0] -band 0xFE) -eq 0xFC) { return $true }

        return $false
    }

    return $false
}

function Get-TcpSummary([int]$procId) {
    $tcp = @(Get-NetTCPConnection -OwningProcess $procId -ErrorAction SilentlyContinue)

    $listen = @($tcp | Where-Object { $_.State -eq 'Listen' }).Count
    $est443 = @($tcp | Where-Object { $_.State -eq 'Established' -and $_.RemotePort -eq 443 }).Count
    $estRedis = @($tcp | Where-Object {
        $_.State -eq 'Established' -and $_.RemoteAddress -eq '127.0.0.1' -and $_.RemotePort -eq 6379
    }).Count

    $estExternal = @(
        $tcp | Where-Object {
            if ($_.State -ne 'Established') { return $false }
            if (-not $_.RemoteAddress) { return $false }
            if ($IncludeLanAsExternal) {
                return ($_.RemoteAddress -notin @('127.0.0.1', '0.0.0.0', '::1'))
            }
            return (-not (Test-IsPrivateOrLoopback $_.RemoteAddress))
        }
    ).Count

    # Any established connection (useful as a “real work” heuristic)
    $estAny = @($tcp | Where-Object { $_.State -eq 'Established' }).Count

    return [pscustomobject]@{
        Listen      = $listen
        Discord443  = $est443
        Redis       = $estRedis
        External    = $estExternal
        Established = $estAny
    }
}

function To-ShortRole([string]$roleName) {
    switch ($roleName) {
        'SLASH_BOT' { return 'SLASH' }
        'DELIVERY_BOT' { return 'BOT' }
        'NEWS_POLLER' { return 'POLL' }
        'REDIS_WORKER' { return 'WORKER' }
        default { return $roleName }
    }
}

function Test-RedisReachable() {
    # NOTE: do not use $host (collides with automatic $Host variable)
    $redisHost = $env:TNT_REDIS_HOST
    if (-not $redisHost) { $redisHost = '127.0.0.1' }
    $redisPort = 6379
    if ($env:TNT_REDIS_PORT -and ($env:TNT_REDIS_PORT -as [int])) { $redisPort = [int]$env:TNT_REDIS_PORT }

    try {
        if (Get-Command Test-NetConnection -ErrorAction SilentlyContinue) {
            return [bool](Test-NetConnection -ComputerName $redisHost -Port $redisPort -InformationLevel Quiet)
        }

        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($redisHost, $redisPort, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(750)
        if (-not $ok) {
            try { $client.Close() } catch {}
            return $false
        }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Shorten([string]$s, [int]$n = 120) {
    if (-not $s) { return '' }
    if ($s.Length -le $n) { return $s }
    return $s.Substring(0, $n - 3) + '...'
}

# Patterns that identify your roles.
# Add/remove as your repo evolves.
$roleMatchers = @(
    @{ Role = 'SLASH_BOT'; Pattern = '(-m\s+cli\.discord_bot)|(\bcli\.discord_bot\b)' },
    @{ Role = 'DELIVERY_BOT'; Pattern = '(-m\s+delivery\.discord_bot)|(\bdelivery\.discord_bot\b)|(\bdelivery\.discord_bot_head\b)' },
    @{ Role = 'NEWS_POLLER'; Pattern = '(news_poller\.py)|(-m\s+scripts\.news_poller)|(\bscripts\.news_poller\b)' },
    @{ Role = 'ALERT_SCHED'; Pattern = '(scripts\\run_alert_scheduler_5m\.py)|(run_alert_scheduler_\d+m\.py)|(tnt_alerts\.scheduler_service)|(tnt_alerts\.scheduler_redis)' },
    @{ Role = 'REDIS_WORKER'; Pattern = '(-m\s+massive_service\.redis_worker)|(\bmassive_service\.redis_worker\b)' },
    @{ Role = 'FUTURES_INGEST'; Pattern = '(futures_ingest_databento\.py)' }
)

$all = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine })

$rows = @()

foreach ($m in $roleMatchers) {
    $role = $m['Role']
    $pat = $m['Pattern']

    $hits = @($all | Where-Object { $_.CommandLine -match $pat })

    foreach ($p in $hits) {
        $tcp = Get-TcpSummary -procId $p.ProcessId
        $roleKey = $role

        if ($role -eq 'ALERT_SCHED') {
            $tf = $null
            if ($p.CommandLine -match 'run_alert_scheduler_(\d+m)\.py') {
                $tf = $Matches[1]
            } elseif ($p.CommandLine -match '(--tf|-Tf)\s+(\S+)') {
                $tf = $Matches[2]
            }
            if ($tf) {
                $roleKey = "ALERT_SCHED:tf=$tf"
            } else {
                # Never collapse schedulers into a shared “unknown” bucket.
                # If arg parsing changes, this prevents accidental dupes.
                $roleKey = "ALERT_SCHED:pid=$($p.ProcessId)"
            }
        }

        if ($role -eq 'REDIS_WORKER') {
            $queue = $null
            if ($p.CommandLine -match '--queue\s+(\S+)') {
                $queue = $Matches[1]
            }
            if (-not $queue) { $queue = 'tnt:jobs' }
            $roleKey = "REDIS_WORKER:queue=$queue"
        }

        if ($role -eq 'FUTURES_INGEST') {
            $roleKey = 'FUTURES_INGEST:databento'
        }

        $activeScore = [int]($tcp.Listen + $tcp.Redis + $tcp.Discord443 + $tcp.External)
        $rows += [pscustomobject]@{
            Role       = $role
            RoleKey    = $roleKey
            PID        = [int]$p.ProcessId
            PPID       = [int]$p.ParentProcessId
            Name       = $p.Name
            Listen     = $tcp.Listen
            Discord443 = $tcp.Discord443
            Redis      = $tcp.Redis
            External   = $tcp.External
            Est        = $tcp.Established
            Active     = $activeScore
            Cmd        = (Shorten $p.CommandLine 140)
        }
    }
}

if ($OnlyRoles -and $OnlyRoles.Count -gt 0) {
    $set = @{}
    foreach ($r in $OnlyRoles) {
        if ($r) { $set[$r.ToUpperInvariant()] = $true }
    }
    $rows = @($rows | Where-Object { $set.ContainsKey($_.Role.ToUpperInvariant()) })
}

$strictShipTonight = ($StrictSingleton -or $StrictTonight)
$exitCode = 0
$dupeCount = 0
$missing = @()

if (-not $rows -or $rows.Count -eq 0) {
    if ($Strict -or $strictShipTonight) {
        Write-Host '[OPS][PROOF][ERROR] No matching python processes found for TNT roles (strict mode).' -ForegroundColor Red
        $exitCode = 3
    } else {
        Write-Host '[OPS][PROOF] No matching python processes found for TNT roles.' -ForegroundColor Yellow
        $exitCode = 0
    }

    $rolesWantedShort = @()
    if ($strictShipTonight) { $rolesWantedShort = @('SLASH','BOT','POLL','WORKER') }
    elseif ($Strict) { $rolesWantedShort = @('SLASH','POLL','WORKER') }
    $lanFlag = if ($IncludeLanAsExternal) { 1 } else { 0 }
    $okFlag = if ($exitCode -eq 0) { 1 } else { 0 }
    Write-Host "[PROOF] ok=$okFlag dupes=0 missing=$($rolesWantedShort.Count) roles=$($rolesWantedShort -join ',') external_lan_included=$lanFlag" -ForegroundColor DarkGray
    exit $exitCode
}

# Print grouped output
Write-Host ''
Write-Host '[OPS][PROOF] Process socket proof (shim vs real):' -ForegroundColor Cyan
$rows |
    Sort-Object RoleKey, PID |
    Format-Table -AutoSize RoleKey, PID, PPID, Listen, Redis, Discord443, External, Est, Active, Cmd

# Decide “realness” in a way that survives detached launchers.
# Some starters create a python.exe shim/launcher whose child python3.x/pythonw is the “real” worker.
# Prefer socket activity heuristics, but also treat the leaf process per RoleKey as REAL.
$rows | Add-Member -NotePropertyName IsReal -NotePropertyValue $false -Force

foreach ($r in $rows) {
    $r.IsReal = (($r.Est -gt 0) -or ($r.Redis -gt 0) -or ($r.Discord443 -gt 0) -or ($r.Listen -gt 0))
}

foreach ($g in ($rows | Group-Object RoleKey)) {
    $grp = @($g.Group)
    if ($grp.Count -eq 0) { continue }

    # Leaf PID = a PID that is not the parent of another process in the same RoleKey group.
    $ppids = @($grp | ForEach-Object { [int]$_.PPID })
    $leafPids = @($grp | Where-Object { $ppids -notcontains [int]$_.PID } | ForEach-Object { [int]$_.PID })
    if ($leafPids.Count -eq 0) {
        # Defensive fallback: if we can't infer the tree, count all as real.
        $leafPids = @($grp | ForEach-Object { [int]$_.PID })
    }

    foreach ($r in $grp) {
        if ($leafPids -contains [int]$r.PID) {
            $r.IsReal = $true
        }
    }
}

# Detect duplicates per RoleKey (two real workers for same key)
# Only singleton-scoped RoleKeys are considered fatal.
$fatalSingletonRoles = @('SLASH_BOT', 'DELIVERY_BOT', 'NEWS_POLLER', 'ALERT_SCHED', 'REDIS_WORKER')
$bad = @()
foreach ($rk in ($rows.RoleKey | Select-Object -Unique)) {
    $real = @($rows | Where-Object { $_.RoleKey -eq $rk -and $_.IsReal })

    $prefix = ($rk -split ':')[0]
    $isSingleton = ($fatalSingletonRoles -contains $prefix)

    if ($isSingleton -and $real.Count -gt 1) {
        $bad += [pscustomobject]@{ RoleKey = $rk; RealCount = $real.Count; PIDs = ($real.PID -join ',') }
    }
}

if ($bad.Count -gt 0) {
    Write-Host ''
    Write-Host '[OPS][PROOF][ERROR] Duplicate REAL instances detected (not shim):' -ForegroundColor Red
    $bad | Format-Table -AutoSize RoleKey, RealCount, PIDs
    $dupeCount = $bad.Count
    $exitCode = 2
} else {
    Write-Host ''
    Write-Host '[OPS][PROOF][OK] No duplicate REAL instances detected. Any extra python.exe rows are shims/launchers.' -ForegroundColor Green
}

$required = @()
if ($strictShipTonight) {
    $required = @('SLASH_BOT', 'DELIVERY_BOT', 'NEWS_POLLER', 'REDIS_WORKER')
} elseif ($Strict) {
    $required = @('SLASH_BOT', 'NEWS_POLLER', 'REDIS_WORKER')
}

if (($Strict -or $strictShipTonight) -and $OnlyRoles -and $OnlyRoles.Count -gt 0) {
    $required = @(
        $OnlyRoles |
            ForEach-Object { $_.ToUpperInvariant() } |
            Where-Object { $_ -in @('SLASH_BOT', 'DELIVERY_BOT', 'NEWS_POLLER', 'REDIS_WORKER') } |
            Select-Object -Unique
    )
}

if ($required.Count -gt 0) {
    foreach ($r in $required) {
        $real = @($rows | Where-Object { $_.Role -eq $r -and $_.IsReal })
        if ($real.Count -eq 0) {
            $missing += $r
        }
    }

    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Host "[OPS][PROOF][ERROR] Missing required singleton role(s): $($missing -join ', ')" -ForegroundColor Red
        if ($exitCode -eq 0) { $exitCode = 3 }
    }
}

if ($strictShipTonight) {
    if (-not (Test-RedisReachable)) {
        Write-Host ''
        Write-Host '[OPS][PROOF][ERROR] Redis unreachable (TNT_REDIS_HOST/TNT_REDIS_PORT).' -ForegroundColor Red
        Write-Host '[OPS][PROOF][HINT] Memurai runs as a Windows service; start/stop typically requires an elevated PowerShell (Run as Administrator).' -ForegroundColor Yellow
        Write-Host '[OPS][PROOF][HINT] Verify elevation: whoami /groups | findstr /i BUILTIN\Administrators' -ForegroundColor Yellow
        # Prefer exit=4 for redis issues (unless we already have dupes=2)
        if ($exitCode -ne 2) { $exitCode = 4 }
    }
}

if ($GrepProofLogs) {
    Write-Host ''
    Write-Host "[OPS][PROOF] Log proof-grep (tail=$Tail):" -ForegroundColor Cyan

    $logFiles = @(
        '.\logs\slash_live.log',
        '.\logs\bot_live.log',
        '.\logs\redis_worker_live.log',
        '.\logs\news_poller_stdout.log'
    ) | Where-Object { Test-Path $_ }

    if ($logFiles.Count -eq 0) {
        Write-Host '[OPS][PROOF] No known log files found under .\logs\ (slash_live/bot_live/redis_worker_live/news_poller_stdout).' -ForegroundColor Yellow
        exit 0
    }

    $pattern = "\[BOOT\]|\[TNT\]\[WORKER\]\[POP\]|\[ALERTS\]\[TRIGGER\]|\[ALERTS\]\[DELIVERY\]|\[NEWS\]\[HEARTBEAT\]|\[EARNINGS_RESULTS\]|\[SMIQ\]|\[WARN\]|\[ERROR\]|publish_id="
    Get-Content -Tail $Tail $logFiles |
        Select-String $pattern |
        Select-Object -Last 220 |
        ForEach-Object { $_.Line }
}

# Pasteable proof line for screenshots / Discord ops posts.
$lanFlag = if ($IncludeLanAsExternal) { 1 } else { 0 }
$okFlag = if ($exitCode -eq 0) { 1 } else { 0 }
$rolesWantedShort = @($required | ForEach-Object { To-ShortRole $_ })
$missingShort = @($missing | ForEach-Object { To-ShortRole $_ })
$rolesWantedJoined = if ($rolesWantedShort.Count -gt 0) { ($rolesWantedShort -join ',') } else { 'n/a' }
Write-Host "[PROOF] ok=$okFlag dupes=$dupeCount missing=$($missingShort.Count) roles=$rolesWantedJoined external_lan_included=$lanFlag" -ForegroundColor DarkGray

exit $exitCode

