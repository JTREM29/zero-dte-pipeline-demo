param(
  [switch]$WithWorker,
  [switch]$WithDelivery,
  [switch]$SkipFutures,
  [switch]$SkipAudit,
  [switch]$RunDiscordCanaries,
  [int]$CanaryTailSeconds = 60,
  [int[]]$AssertPort = @(),
  [switch]$KillBoundPorts,
  [switch]$StrictRepo,
  [int]$BotReadyTimeoutSeconds = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step {
  param([Parameter(Mandatory=$true)][string]$Label)
  Write-Host "`n=== $Label ===" -ForegroundColor Cyan
}

function Run-File {
  param(
    [Parameter(Mandatory=$true)][string]$Label,
    [Parameter(Mandatory=$true)][string]$Path,
    [string[]]$Args = @(),
    [switch]$AllowFailure
  )

  Write-Step $Label
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Missing required script: $Path"
  }

  & powershell -NoProfile -ExecutionPolicy Bypass -File $Path @Args
  $code = $LASTEXITCODE

  if (-not $AllowFailure -and $code -ne 0) {
    throw "[TNT] '$Label' failed (exit $code)"
  }

  return $code
}

function Start-Supervisor {
  [CmdletBinding(SupportsShouldProcess = $true)]
  param(
    [Parameter(Mandatory=$true)][string]$Label,
    [Parameter(Mandatory=$true)][string]$ScriptPath,
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$PythonExe
  )

  Write-Step $Label
  if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Missing supervisor script: $ScriptPath"
  }

  if (-not $PSCmdlet.ShouldProcess($ScriptPath, "Start supervisor")) {
    return
  }

  $p = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $ScriptPath,
        "-PythonExe", $PythonExe
      ) -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru

  Write-Host "[OPS] started supervisor pid=$($p.Id) script=$(Split-Path -Leaf $ScriptPath)" -ForegroundColor Green
  Start-Sleep -Seconds 4
}

function Stop-ExtraTntProcess {
  [CmdletBinding(SupportsShouldProcess = $true)]
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot
  )

  Write-Step "Stop extra TNT processes (best-effort)"

  $escapedRepo = [regex]::Escape($RepoRoot)
  $rx = "context_writer_loop\\.py|earnings_scheduler\\.py|polygon_ingest\\.py|(-m\\s+scripts\\.context_writer_loop)|(-m\\s+scripts\\.earnings_scheduler)"

  $procs = @(Get-CimInstance Win32_Process | Where-Object {
      $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $escapedRepo) -and ($_.CommandLine -match $rx)
    })

  if (-not $procs -or $procs.Count -eq 0) {
    Write-Host "[OPS] none" -ForegroundColor DarkGray
    return
  }

  foreach ($p in $procs) {
    try {
      Write-Host "[OPS] stopping pid=$($p.ProcessId)" -ForegroundColor Yellow
      if ($PSCmdlet.ShouldProcess("pid=$($p.ProcessId)", "Stop-Process")) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      }
    } catch {
      # ignore
    }
  }
}

function Assert-OneBotRoot {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [int]$TimeoutSeconds = 25
  )

  Write-Step "Assert single bot instance"

  $escapedRepo = [regex]::Escape($RepoRoot)
  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))

  $procs = @()
  while ((Get-Date) -lt $deadline) {
    $procs = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -match $escapedRepo) -and ($_.CommandLine -match '(cli\.discord_bot|delivery\.discord_bot)')
      })
    if ($procs -and $procs.Count -gt 0) {
      break
    }
    Start-Sleep -Milliseconds 500
  }

  if (-not $procs -or $procs.Count -eq 0) {
    throw "[TNT] No bot processes found after start (timeout ${TimeoutSeconds}s)."
  }

  # Treat python.exe -> python3.x.exe as one instance by counting only root processes.
  $pidSet = @{}
  foreach ($p in $procs) { $pidSet[[int]$p.ProcessId] = $true }
  $roots = @($procs | Where-Object { -not $pidSet.ContainsKey([int]$_.ParentProcessId) })

  foreach ($p in $procs | Sort-Object CreationDate) {
    try {
      Write-Host ("[OPS] bot pid={0} ppid={1} cmd={2}" -f $p.ProcessId, $p.ParentProcessId, ($p.CommandLine.Substring(0, [Math]::Min(140, $p.CommandLine.Length)))) -ForegroundColor DarkGray
    } catch {
      # ignore
    }
  }

  $rootCount = ($roots | Measure-Object).Count
  if ($rootCount -gt 1) {
    $ids = @($roots | ForEach-Object { [string]$_.ProcessId })
    throw "[TNT] Multiple bot roots detected (roots=$rootCount pids=$($ids -join ',')). Stop extras and retry."
  }

  Write-Host "[OK] bot root count=$rootCount" -ForegroundColor Green
}

function Test-RepoDirty {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [switch]$Strict
  )

  Write-Step "Repo dirty check"
  Set-Location $RepoRoot

  $dirty = $null
  try {
    $dirty = @(git status --porcelain 2>$null)
  } catch {
    $dirty = $null
  }

  if (-not $dirty -or $dirty.Count -eq 0) {
    Write-Host "[OK] repo clean" -ForegroundColor Green
    return
  }

  Write-Host "[TNT] WARNING: repo has uncommitted changes:" -ForegroundColor Yellow
  $dirty | Select-Object -First 25 | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
  if ($dirty.Count -gt 25) {
    Write-Host "[TNT] (showing first 25 of $($dirty.Count))" -ForegroundColor DarkGray
  }

  if ($Strict) {
    throw "Repo dirty. Refuse converge in -StrictRepo mode."
  }
}

function Wait-BotReady {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [int]$TimeoutSeconds = 25,
    [hashtable]$Baselines = @{}
  )

  Write-Step "Wait for bot readiness (log proof)"

  $paths = @(
    (Join-Path $RepoRoot "logs\bot_supervised_child_stdout.log"),
    (Join-Path $RepoRoot "logs\slash_live.log")
  )

  $patterns = @(
    "\\[TNT\\]\\[PROOF\\]",
    "Logged in as",
    "\\[TNT\\]\\[START\\]"
  )

  $buffers = @{}
  foreach ($p in $paths) { $buffers[$p] = "" }

  $rotNoted = @{}

  $deadline = (Get-Date).AddSeconds([Math]::Max(3, $TimeoutSeconds))
  while ((Get-Date) -lt $deadline) {
    foreach ($p in $paths) {
      try {
        if (-not (Test-Path -LiteralPath $p)) {
          continue
        }

        $base = 0L
        if ($Baselines -and $Baselines.ContainsKey($p)) {
          try { $base = [int64]$Baselines[$p] } catch { $base = 0L }
        }

        $fi = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
        if (-not $fi) { continue }

        $len = 0L
        try { $len = [int64]$fi.Length } catch { $len = 0L }

        # Rotation/truncation: reset baseline.
        if ($len -lt $base) {
          if (-not $rotNoted.ContainsKey($p)) {
            try {
              Write-Host ("bot_ready_log_rotated=1 baseline_reset=1 source={0} old_len={1} new_len={2}" -f (Split-Path -Leaf $p), $base, $len) -ForegroundColor DarkGray
            } catch {
              # ignore
            }
            $rotNoted[$p] = $true
          }
          $base = 0L
          if ($Baselines) { $Baselines[$p] = 0L }
        }

        if ($len -le $base) {
          continue
        }

        $fs = $null
        try {
          $fs = [System.IO.File]::Open($p, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
          [void]$fs.Seek($base, [System.IO.SeekOrigin]::Begin)
          # Use a non-throwing UTF-8 decoder; we may start mid-character.
          $enc = New-Object System.Text.UTF8Encoding($false, $false)
          $sr = New-Object System.IO.StreamReader($fs, $enc, $true)
          $txt = $sr.ReadToEnd()
          $sr.Dispose()

          # Update baseline to current file end.
          if ($Baselines) { $Baselines[$p] = $len }

          if (-not $txt) { continue }

          $combined = "" + ($buffers[$p] | ForEach-Object { $_ }) + $txt
          $lines = $combined -split "`r?`n"

          # If last chunk didn't end with newline, keep trailing partial line.
          if (-not ($combined.EndsWith("`n") -or $combined.EndsWith("`r"))) {
            $buffers[$p] = $lines[-1]
            if ($lines.Count -gt 1) {
              $lines = $lines[0..($lines.Count-2)]
            } else {
              $lines = @()
            }
          } else {
            $buffers[$p] = ""
          }

          foreach ($line in $lines) {
            if (-not $line) { continue }
            foreach ($rx in $patterns) {
              if ($line -match $rx) {
                $src = Split-Path -Leaf $p
                $m = $line.Trim()
                Write-Host ("[OK] bot ready: {0}" -f $m) -ForegroundColor Green
                Write-Host ("bot_ready_source={0}" -f $src) -ForegroundColor DarkGray
                Write-Host ("bot_ready_match={0}" -f $m) -ForegroundColor DarkGray
                return [pscustomobject]@{ Source = $src; Match = $m }
              }
            }
          }
        } finally {
          if ($fs) { try { $fs.Dispose() } catch { } }
        }
      } catch {
        # ignore
      }
    }
    Start-Sleep -Milliseconds 350
  }

  throw "[TNT] Bot did not emit readiness proof lines within ${TimeoutSeconds}s (fresh-only scan). Check logs under .\\logs\\ for startup errors."
}

function Get-LogBaselines {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot
  )

  $paths = @(
    (Join-Path $RepoRoot "logs\bot_supervised_child_stdout.log"),
    (Join-Path $RepoRoot "logs\slash_live.log")
  )

  $h = @{}
  foreach ($p in $paths) {
    try {
      if (Test-Path -LiteralPath $p) {
        $fi = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
        if ($fi) {
          $h[$p] = [int64]$fi.Length
        } else {
          $h[$p] = 0L
        }
      } else {
        $h[$p] = 0L
      }
    } catch {
      $h[$p] = 0L
    }
  }
  return $h
}

function Get-PortListeners {
  param(
    [Parameter(Mandatory=$true)][int]$Port
  )

  try {
    $cmd = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($cmd) {
      $hits = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
      return @($hits | ForEach-Object {
          [pscustomobject]@{ Port = $Port; Pid = [int]$_.OwningProcess; Line = "LocalAddress=$($_.LocalAddress) LocalPort=$($_.LocalPort) State=$($_.State) OwningProcess=$($_.OwningProcess)" }
        })
    }
  } catch {
    # fall through
  }

  $lines = @(netstat -ano | Select-String ":$Port\s+LISTENING")
  return @($lines | ForEach-Object {
      $line = $_.Line.Trim()
      $pid = 0
      try { $pid = [int]($line -split "\s+")[-1] } catch { $pid = 0 }
      [pscustomobject]@{ Port = $Port; Pid = $pid; Line = $line }
    })
}

function Assert-PortFree {
  param(
    [Parameter(Mandatory=$true)][int]$Port,
    [switch]$Kill
  )

  $hits = @(Get-PortListeners -Port $Port)
  if (-not $hits -or $hits.Count -eq 0) {
    Write-Host "[OK] port $Port free" -ForegroundColor Green
    return
  }

  $hitLines = @($hits | ForEach-Object { $_.Line })

  if (-not $Kill) {
    throw "Port $Port already LISTENING. Refuse start. ($($hitLines -join ' | '))"
  }

  $pids = @($hits | Where-Object { $_.Pid -gt 0 } | Select-Object -ExpandProperty Pid -Unique)
  if (-not $pids -or $pids.Count -eq 0) {
    throw "Port $Port already LISTENING, but owning PID unavailable. Refuse start. ($($hitLines -join ' | '))"
  }

  Write-Host "[OPS] port $Port LISTENING; stopping owner pid(s)=$($pids -join ',')" -ForegroundColor Yellow
  foreach ($pid in $pids) {
    try { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue } catch { }
  }

  Start-Sleep -Milliseconds 400
  $after = @(Get-PortListeners -Port $Port)
  if ($after -and $after.Count -gt 0) {
    $afterLines = @($after | ForEach-Object { $_.Line })
    throw "Port $Port still LISTENING after kill attempt. Refuse start. ($($afterLines -join ' | '))"
  }
  Write-Host "[OK] port $Port freed" -ForegroundColor Green
}

function Assert-PortListening {
  param(
    [Parameter(Mandatory=$true)][int]$Port
  )

  $hits = @(Get-PortListeners -Port $Port)
  if ($hits -and $hits.Count -gt 0) {
    $pids = @($hits | Where-Object { $_.Pid -gt 0 } | Select-Object -ExpandProperty Pid -Unique)
    $pidStr = ""
    if ($pids -and $pids.Count -gt 0) {
      $pidStr = " pid=" + ($pids -join ",")
    }
    Write-Host ("[OK] port {0} listening{1}" -f $Port, $pidStr) -ForegroundColor Green
    return
  }
  throw "Port $Port is not LISTENING. Expected service not running."
}

function Tail-DiscordCanaryProof {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [int]$Seconds = 60
  )

  $paths = @(
    (Join-Path $RepoRoot "logs\bot_supervised_child_stdout.log"),
    (Join-Path $RepoRoot "logs\slash_live.log")
  ) | Where-Object { Test-Path -LiteralPath $_ }

  if (-not $paths -or $paths.Count -eq 0) {
    Write-Host "[TNT][CANARY] no bot log files found to tail" -ForegroundColor Yellow
    return
  }

  $patterns = @(
    "\\[TNT\\]\\[ASK\\]",
    "\\[TNT\\]\\[MACRO_PULSE\\]",
    "\\[TNT\\]\\[OI\\]\\[PROOF\\]"
  )

  $startLocal = Get-Date
  $startUtc = $startLocal.ToUniversalTime()
  $effectiveSeconds = [Math]::Max(5, $Seconds)
  $endUtc = $startUtc.AddSeconds($effectiveSeconds)
  Write-Host ("`n[TNT][CANARY] capture_window_utc_start={0} capture_window_utc_end={1} seconds={2}" -f ($startUtc.ToString('o')), ($endUtc.ToString('o')), $effectiveSeconds) -ForegroundColor Yellow
  Write-Host ("[TNT][CANARY] {0} Run the 3 Discord canaries now; capturing proof lines for {1}s..." -f ($startLocal.ToString('o')), $effectiveSeconds) -ForegroundColor Yellow

  $seen = @{}
  $deadline = (Get-Date).AddSeconds($effectiveSeconds)
  while ((Get-Date) -lt $deadline) {
    foreach ($p in $paths) {
      try {
        $tail = @(Get-Content -LiteralPath $p -Tail 250 -ErrorAction SilentlyContinue)
        foreach ($line in $tail) {
          if (-not $line) { continue }
          $hit = $false
          foreach ($rx in $patterns) {
            if ($line -match $rx) { $hit = $true; break }
          }
          if (-not $hit) { continue }
          if ($seen.ContainsKey($line)) { continue }
          $seen[$line] = $true

          $t = "TNT"
          if ($line -match "\\[TNT\\]\\[ASK\\]") { $t = "ASK" }
          elseif ($line -match "\\[TNT\\]\\[MACRO_PULSE\\]") { $t = "MACRO_PULSE" }
          elseif ($line -match "\\[TNT\\]\\[OI\\]\\[PROOF\\]") { $t = "OI" }

          $meta = @()
          try {
            if ($line -match "channel_id=(\\d+)") { $meta += ("channel_id=$($Matches[1])") }
          } catch { }
          try {
            if ($line -match "target_channel_id=(\\d+)") { $meta += ("target_channel_id=$($Matches[1])") }
          } catch { }
          try {
            if ($line -match "symbol=([A-Za-z0-9_\.-]+)") { $meta += ("symbol=$($Matches[1])") }
          } catch { }
          try {
            if ($line -match "sym=([A-Za-z0-9_\.-]+)") { $meta += ("sym=$($Matches[1])") }
          } catch { }
          try {
            if ($line -match "worker=([^\s]+)") { $meta += ("worker=$($Matches[1])") }
          } catch { }

          $metaStr = ""
          if ($meta -and $meta.Count -gt 0) {
            $metaStr = " " + ($meta -join " ")
          }

          Write-Host ("[PROOF][{0}]{1} {2}" -f $t, $metaStr, $line.Trim()) -ForegroundColor DarkGray
        }
      } catch {
        # ignore
      }
    }
    Start-Sleep -Seconds 1
  }

  Write-Host "[TNT][CANARY] capture window complete." -ForegroundColor Cyan
}

# 0) Ensure repo root (script can be run from anywhere)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..") | Select-Object -First 1 -ExpandProperty Path)
Set-Location $RepoRoot

Test-RepoDirty -RepoRoot $RepoRoot -Strict:$StrictRepo

$pythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
  $pythonExe = "python"
}

Write-Host "`n[TNT] Converge to GREEN starting in: $RepoRoot" -ForegroundColor Green
Write-Host "[TNT] python=$pythonExe" -ForegroundColor DarkGray
try {
  $nowLocal = Get-Date
  $nowUtc = $nowLocal.ToUniversalTime()
  Write-Host ("[TNT] now_local={0} now_utc={1}" -f ($nowLocal.ToString('o')), ($nowUtc.ToString('o'))) -ForegroundColor DarkGray
} catch { }

if ($SkipFutures -and (-not $SkipAudit)) {
  Write-Host "[TNT][WARN] -SkipFutures with audit enabled will likely fail Connected Audit (futures is part of GREEN). Consider -SkipAudit too." -ForegroundColor Yellow
}

if ($WithDelivery) {
  Write-Host "[TNT][WARN] -WithDelivery starts delivery.discord_bot; if it shares the same DISCORD_BOT_TOKEN as cli.discord_bot, the singleton lock will prevent parallel runs." -ForegroundColor Yellow
}

# 1) Stop everything TNT-related (safe)
Run-File "Stop TNT (all)" (Join-Path $RepoRoot "scripts\stop_tnt_all.ps1") -Args @("-IncludeSupervisors") -AllowFailure
Stop-ExtraTntProcess -RepoRoot $RepoRoot
Start-Sleep -Seconds 2

# 1.5) Optional: fail-fast if ports are already bound (defensive)
if ($AssertPort -and $AssertPort.Count -gt 0) {
  Write-Step "Assert ports free"
  foreach ($p in $AssertPort) {
    $port = [int]$p
    # Special-case: Redis on CLX is expected to be LISTENING (Memurai service).
    if ($port -eq 6379) {
      Assert-PortListening -Port $port
      continue
    }

    Assert-PortFree -Port $port -Kill:$KillBoundPorts
  }
}

# 2) Start ONE supervised cli bot
$botLogBaselines = Get-LogBaselines -RepoRoot $RepoRoot
Start-Supervisor "Start supervised cli.discord_bot" (Join-Path $RepoRoot "run_discord_supervised.ps1") -RepoRoot $RepoRoot -PythonExe $pythonExe
Assert-OneBotRoot -RepoRoot $RepoRoot
Wait-BotReady -RepoRoot $RepoRoot -TimeoutSeconds $BotReadyTimeoutSeconds -Baselines $botLogBaselines

# 3) Optional: delivery bot (only if you intend to run it separately)
if ($WithDelivery) {
  Run-File "Start delivery.discord_bot (detached)" (Join-Path $RepoRoot "scripts\start_delivery_bot_bg.ps1") -Args @("-PythonExe", $pythonExe, "-LogDir", (Join-Path $RepoRoot "logs"))
}

# 4) Futures ingest (supervised unless skipped)
if (-not $SkipFutures) {
  Start-Supervisor "Start supervised futures ingest (Databento)" (Join-Path $RepoRoot "run_futures_supervised.ps1") -RepoRoot $RepoRoot -PythonExe $pythonExe
}

# 5) Optional: worker-ish helpers (redis worker + news poller)
if ($WithWorker) {
  Run-File "Start massive_service.redis_worker (detached)" (Join-Path $RepoRoot "scripts\start_redis_worker_bg.ps1") -Args @("-PythonExe", $pythonExe, "-LogDir", (Join-Path $RepoRoot "logs"))
  Run-File "Start news poller (detached)" (Join-Path $RepoRoot "scripts\start_news_poller_bg.ps1") -Args @("-PythonExe", $pythonExe, "-LogDir", (Join-Path $RepoRoot "logs"))
}

Start-Sleep -Seconds 3

# 6) Audit (unless skipped)
if (-not $SkipAudit) {
  Write-Step "Connected Audit (SSoT)"
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\connected_audit_one_shot.ps1")
  $code = $LASTEXITCODE
  if ($code -ne 0) {
    Write-Host "[TNT] Connected Audit FAILED (exit $code). Fix DEGRADED items before proceeding." -ForegroundColor Red
    exit $code
  }
}

Write-Host "`n[TNT] GREEN converge complete." -ForegroundColor Green

Write-Host "`n[TNT] Build fingerprints" -ForegroundColor Cyan
Set-Location $RepoRoot
try {
  $head = (git rev-parse --short HEAD 2>$null)
  if ($head) {
    Write-Host ("repo_head={0}" -f $head.Trim()) -ForegroundColor DarkGray
  }
} catch {
  # ignore
}

try {
  $pyCmd = (Get-Command python -ErrorAction SilentlyContinue)
  if ($pyCmd -and $pyCmd.Source) {
    Write-Host ("python={0}" -f $pyCmd.Source) -ForegroundColor DarkGray
  } else {
    Write-Host "python=(not found on PATH)" -ForegroundColor DarkGray
  }
} catch {
  # ignore
}

Write-Host ("python_exe={0}" -f $pythonExe) -ForegroundColor DarkGray

try {
  $proof = $null
  $start = $null
  $p1 = Join-Path $RepoRoot "logs\bot_supervised_child_stdout.log"
  $p2 = Join-Path $RepoRoot "logs\slash_live.log"
  if (Test-Path -LiteralPath $p1) {
    $proof = Select-String -LiteralPath $p1 -Pattern "\[TNT\]\[PROOF\]" | Select-Object -Last 1
    $start = Select-String -LiteralPath $p1 -Pattern "\[TNT\]\[START\]" | Select-Object -Last 1
  }
  if (-not $proof -and (Test-Path -LiteralPath $p2)) {
    $proof = Select-String -LiteralPath $p2 -Pattern "\[TNT\]\[PROOF\]" | Select-Object -Last 1
    if (-not $start) {
      $start = Select-String -LiteralPath $p2 -Pattern "\[TNT\]\[START\].*build=" | Select-Object -Last 1
      if (-not $start) {
        $start = Select-String -LiteralPath $p2 -Pattern "\[TNT\]\[START\]" | Select-Object -Last 1
      }
    }
  }
  if ($start) {
    Write-Host ("bot_start_line={0}" -f ($start.Line.Trim())) -ForegroundColor DarkGray
  }
  if ($proof) {
    Write-Host ("bot_proof={0}" -f ($proof.Line.Trim())) -ForegroundColor DarkGray
  } else {
    Write-Host "bot_proof=(missing; restart bot / check logs)" -ForegroundColor Yellow
  }
} catch {
  Write-Host "bot_proof=(error reading logs)" -ForegroundColor Yellow
}

Write-Host "`nManual Discord canaries:" -ForegroundColor Cyan
Write-Host "1) #ask-tnt:           macro posture?"
Write-Host "2) #calendar-earnings: /macro_pulse"
Write-Host "3) anywhere:           /oi SPY"

if ($RunDiscordCanaries) {
  Tail-DiscordCanaryProof -RepoRoot $RepoRoot -Seconds $CanaryTailSeconds
}

exit 0
