param(
  [switch]$Strict,
  [switch]$AsDiscordCodeBlock
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..") | Select-Object -First 1 -ExpandProperty Path)
Set-Location $RepoRoot

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$conv = Join-Path $RepoRoot "scripts\converge_green.ps1"
if (-not (Test-Path -LiteralPath $conv)) {
  throw "Missing converge script: $conv"
}

$convArgs = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", $conv,
  "-AssertPort", "6379",
  "-RunDiscordCanaries",
  "-CanaryTailSeconds", "60",
  "-BotReadyTimeoutSeconds", "45"
)
if ($Strict) {
  $convArgs += "-StrictRepo"
}

function _Tail-Lines {
    param(
      [Parameter(Mandatory=$true)][string]$Path,
      [int]$Tail = 250
    )
    try {
      if (-not (Test-Path -LiteralPath $Path)) { return @() }
      return @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue -Tail $Tail)
    } catch {
      return @()
    }
}

function _Find-LastMatch {
    param(
      [Parameter(Mandatory=$true)][string[]]$Lines,
      [Parameter(Mandatory=$true)][string[]]$Patterns
    )
    for ($i = $Lines.Count - 1; $i -ge 0; $i--) {
      $line = $Lines[$i]
      if (-not $line) { continue }
      foreach ($p in $Patterns) {
        if ($line -match $p) {
          return $line.Trim()
        }
      }
    }
    return $null
}

function _LastMatchIndex {
    param(
      [Parameter(Mandatory=$true)][string[]]$Lines,
      [Parameter(Mandatory=$true)][string]$Pattern
    )
    for ($i = $Lines.Count - 1; $i -ge 0; $i--) {
      $line = $Lines[$i]
      if (-not $line) { continue }
      if ($line -match $Pattern) { return $i }
    }
    return -1
}

function _Slice-AfterLast {
    param(
      [Parameter(Mandatory=$true)][string[]]$Lines,
      [Parameter(Mandatory=$true)][string]$Pattern
    )
    $idx = _LastMatchIndex -Lines $Lines -Pattern $Pattern
    if ($idx -lt 0) { return $Lines }
    return $Lines[$idx..($Lines.Count - 1)]
}

function _Get-PrimaryIPv4 {
    try {
      $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object -Property RouteMetric |
        Select-Object -First 1
      if ($route -and $route.ifIndex) {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.ifIndex -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -and $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notmatch '^169\.254\.' } |
          Select-Object -First 1
        if ($ip -and $ip.IPAddress) { return $ip.IPAddress }
      }
    } catch { }

    try {
      $ip2 = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -and $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notmatch '^169\.254\.' } |
        Sort-Object -Property InterfaceMetric |
        Select-Object -First 1
      if ($ip2 -and $ip2.IPAddress) { return $ip2.IPAddress }
    } catch { }

    return "(unknown)"
}

function _Get-WorkerHealthOneLine {
    param(
      [Parameter(Mandatory=$true)][string]$RepoRoot
    )
    $worker = (Get-Item -Path Env:TNT_WORKER_URL -ErrorAction SilentlyContinue).Value
    if ([string]::IsNullOrWhiteSpace($worker)) {
      $worker = "http://192.168.1.145:8787"
    }
    $base = ("$worker").Trim().TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($base)) {
      return "worker_healthz=missing"
    }

    $url = "$base/healthz"
    try {
      $hz = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 3
      if (-not $hz) { return "worker_healthz=fail url=$url err=no_json" }
      $ok = $false
      try { $ok = [bool]$hz.ok } catch { $ok = $false }
      if (-not $ok) { return "worker_healthz=fail url=$url err=not_ok" }

      $build = ""
      $uptime = ""
      $service = ""
      try { if ($hz.build) { $build = "$($hz.build)" } } catch { }
      try { if ($hz.uptime_s) { $uptime = "$($hz.uptime_s)" } } catch { }
      try { if ($hz.service) { $service = "$($hz.service)" } } catch { }
      if (-not $service) {
        try { if ($hz.name) { $service = "$($hz.name)" } } catch { }
      }

      $line = "worker_healthz=ok url=$url"
      if ($service) { $line += " service=$service" }
      if ($build) { $line += " build=$build" }
      if ($uptime) { $line += " uptime_s=$uptime" }
      return $line
    } catch {
      $msg = ($_.Exception.Message | Out-String).Trim()
      if ($msg.Length -gt 180) { $msg = $msg.Substring(0, 180) }
      return "worker_healthz=fail url=$url err=$msg"
    }
}

function _Get-RedisListening {
    param([int]$Port)
    try {
      $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($c -and $c.OwningProcess) {
        return "redis_listen=$Port pid=$($c.OwningProcess)"
      }
    } catch { }
    return "redis_listen=$Port pid=(none)"
}

function _Run-ConnectedAuditCapture {
    param([Parameter(Mandatory=$true)][string]$RepoRoot)
    $ps1 = Join-Path $RepoRoot "scripts\connected_audit_one_shot.ps1"
    $py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $ps1)) {
      return [pscustomobject]@{ ExitCode = 1; Lines = @("[WARN] connected audit wrapper missing: $ps1") }
    }

    try {
      $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $ps1 -RepoRoot $RepoRoot -PythonExe $py 2>&1
      $ec = $LASTEXITCODE
      $lines = @($out)
      if ($ec -ne 0) {
        $lines += "[FAIL] connected audit exit=$ec"
      }
      return [pscustomobject]@{ ExitCode = $ec; Lines = $lines }
    } catch {
      return [pscustomobject]@{ ExitCode = 1; Lines = @("[FAIL] connected audit exception: $($_.Exception.Message)") }
    }
}

Write-Host "[TNT] E2E Proof starting (evidence from logs + connected audit stdout)" -ForegroundColor Cyan
try {
  & powershell @convArgs
  $code = $LASTEXITCODE
} catch {
  $code = 1
}

# Build paste-ready evidence from stable sources (logs + Python audit stdout).
$botLog = Join-Path $RepoRoot "logs\bot_supervised_child_stdout.log"
$slashLog = Join-Path $RepoRoot "logs\slash_live.log"

$botLines = _Tail-Lines -Path $botLog -Tail 400
$slashLines = _Tail-Lines -Path $slashLog -Tail 400

# Stale-proofing: only accept evidence after the latest bot start marker.
$botRun = _Slice-AfterLast -Lines $botLines -Pattern '\\[TNT\\]\\[START\\]'
$slashRun = _Slice-AfterLast -Lines $slashLines -Pattern '\\[TNT\\]\\[START\\]'
$runLines = @($botRun + $slashRun)

$readyPatterns = @("\\[TNT\\]\\[PROOF\\]", "Logged in as", "\\[TNT\\]\\[START\\]")
$botReady = _Find-LastMatch -Lines $botRun -Patterns $readyPatterns
$slashReady = _Find-LastMatch -Lines $slashRun -Patterns $readyPatterns

$readyLine = $botReady
$readySrc = "bot_supervised_child_stdout.log"
if (-not $readyLine -and $slashReady) {
  $readyLine = $slashReady
  $readySrc = "slash_live.log"
}

$startLine = _Find-LastMatch -Lines $runLines -Patterns @("\\[TNT\\]\\[START\\]")
$proofLine = _Find-LastMatch -Lines $runLines -Patterns @("\\[TNT\\]\\[PROOF\\]")

$repoHead = ""
try {
  $repoHead = (git rev-parse --short HEAD 2>$null)
  if ($repoHead) { $repoHead = $repoHead.Trim() }
} catch { }

$auditObj = _Run-ConnectedAuditCapture -RepoRoot $RepoRoot
$auditOut = @($auditObj.Lines)

# Pull the config header block verbatim.
$auditCfg = @()
for ($i = 0; $i -lt $auditOut.Count; $i++) {
  $line = "$($auditOut[$i])"
  if ($line -match '^\[AUDIT\] config:') {
    $auditCfg += $line.TrimEnd()
    for ($j = $i + 1; $j -lt $auditOut.Count; $j++) {
      $n = "$($auditOut[$j])"
      if ($n -match '^\s{2,}') {
        $auditCfg += $n.TrimEnd()
        continue
      }
      break
    }
    break
  }
}

$auditFutures = _Find-LastMatch -Lines $auditOut -Patterns @('^\[OK\] futures proof:')
$auditWorker = _Find-LastMatch -Lines $auditOut -Patterns @('^\[OK\] worker render:')

# Canary proofs from the current bot run only (post-[TNT][START]).
# These proofs may land in either bot_supervised_child_stdout.log or slash_live.log.
$askRoute = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[ASK\]\[ROUTE\]')
$macroPulse = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[MACRO_PULSE\]')
$oiProof = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[OI\]\[PROOF\]')

# Late-proof grace: /oi can take a few seconds to complete and emit proof lines.
if ((-not $askRoute) -or (-not $macroPulse) -or (-not $oiProof)) {
  $graceSeconds = 8
  for ($g = 0; $g -lt $graceSeconds; $g++) {
    Start-Sleep -Seconds 1

    $botLines = _Tail-Lines -Path $botLog -Tail 500
    $slashLines = _Tail-Lines -Path $slashLog -Tail 500

    $botRun = _Slice-AfterLast -Lines $botLines -Pattern '\\[TNT\\]\\[START\\]'
    $slashRun = _Slice-AfterLast -Lines $slashLines -Pattern '\\[TNT\\]\\[START\\]'
    $runLines = @($botRun + $slashRun)

    if (-not $askRoute) { $askRoute = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[ASK\]\[ROUTE\]') }
    if (-not $macroPulse) { $macroPulse = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[MACRO_PULSE\]') }
    if (-not $oiProof) { $oiProof = _Find-LastMatch -Lines $runLines -Patterns @('\[TNT\]\[OI\]\[PROOF\]') }

    if ($askRoute -and $macroPulse -and $oiProof) { break }
  }
}

$lines = @()
$computerName = $env:COMPUTERNAME
if ([string]::IsNullOrWhiteSpace($computerName)) { $computerName = "(unknown)" }
$ip = _Get-PrimaryIPv4
$lines += "[TNT] e2e_proof ts=$ts host=$computerName ip=$ip repo_head=$repoHead exit=$code"
$lines += (_Get-RedisListening -Port 6379)
$lines += (_Get-WorkerHealthOneLine -RepoRoot $RepoRoot)
if ($readyLine) {
  $lines += "[OK] bot_ready_source=$readySrc"
  $lines += "[OK] bot_ready_match=$readyLine"
} else {
  $lines += "[WARN] bot_ready_match not found (check logs/*.log)"
}
if ($startLine) { $lines += "[OK] bot_start_line=$startLine" }
if ($proofLine) { $lines += "[OK] bot_proof=$proofLine" }

$lines += "[AUDIT] ConnectedAudit exit_code=$($auditObj.ExitCode)"

if ($auditCfg -and $auditCfg.Count -gt 0) {
  $lines += $auditCfg
}
if ($auditFutures) {
  $lines += $auditFutures
}
if ($auditWorker) {
  $lines += $auditWorker
}

if ($askRoute) { $lines += $askRoute }
if ($macroPulse) { $lines += $macroPulse }
if ($oiProof) { $lines += $oiProof }

$macroTag = ""
try {
  if ($macroPulse -and ($macroPulse -match '\\[TNT\\]\\[MACRO_PULSE\\]\\[([A-Z_]+)\\]')) { $macroTag = $Matches[1] }
} catch { }

$askHit = 0
$macroHit = 0
$oiHit = 0
if ($askRoute) { $askHit = 1 }
if ($macroPulse) { $macroHit = 1 }
if ($oiProof) { $oiHit = 1 }

$macroSuffix = ""
if ($macroTag) { $macroSuffix = "($macroTag)" }
$lines += ("[OK] canaries=ask:{0} macro:{1}{2} oi:{3}" -f $askHit, $macroHit, $macroSuffix, $oiHit)

$tailN = 15
if ($lines.Count -gt $tailN) {
  $lines = $lines[($lines.Count - $tailN)..($lines.Count - 1)]
}

$out = ($lines -join "`n").TrimEnd()
if ($AsDiscordCodeBlock) {
  $out = '```' + "`n" + $out + "`n" + '```'
}

try { Set-Clipboard -Value $out } catch { }

Write-Host "`n[TNT] E2E Proof (last $tailN lines; copied to clipboard):" -ForegroundColor Green
Write-Output $out

exit $code
