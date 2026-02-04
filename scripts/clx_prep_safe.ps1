param(
    [string]$WorkerRoot = 'C:\tnt\workers',
    [string]$WorkerName = 'dell_w1',
    [string]$RedisHost = $env:TNT_REDIS_HOST,
    [int]$RedisPort = 0,
    [string]$RedisCliPath = $env:TNT_REDIS_CLI
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $RedisHost -or -not $RedisHost.Trim()) {
    $RedisHost = '127.0.0.1'
}

try {
    if (-not $RedisPort -or $RedisPort -le 0) {
        $RedisPort = [int]($env:TNT_REDIS_PORT)
    }
} catch {
    $RedisPort = 0
}
if (-not $RedisPort -or $RedisPort -le 0) {
    $RedisPort = 6379
}

$workerDir = Join-Path $WorkerRoot $WorkerName

Write-Host "[CLX-PREP] Creating worker folder structure (no installs, no services)" -ForegroundColor Cyan
Write-Host "[CLX-PREP]   root=$WorkerRoot" -ForegroundColor DarkGray
Write-Host "[CLX-PREP]   worker=$workerDir" -ForegroundColor DarkGray

New-Item -ItemType Directory -Force -Path $WorkerRoot | Out-Null
New-Item -ItemType Directory -Force -Path $workerDir | Out-Null

Write-Host "[CLX-PREP] OK folder structure ready" -ForegroundColor Green

Write-Host
Write-Host "[CLX-PREP] Redis read-only check: INFO memory" -ForegroundColor Cyan
Write-Host ("[CLX-PREP]   target={0}:{1}" -f $RedisHost, $RedisPort) -ForegroundColor DarkGray

$redisCliExe = $null

if ($RedisCliPath -and $RedisCliPath.Trim()) {
    $p = $RedisCliPath.Trim('"').Trim()
    if (Test-Path -LiteralPath $p) {
        $redisCliExe = $p
    } else {
        Write-Host ("[CLX-PREP][WARN] TNT_REDIS_CLI provided but not found: {0}" -f $p) -ForegroundColor Yellow
    }
}

if (-not $redisCliExe) {
    $candidates = @(
        'C:\Program Files\Memurai\redis-cli.exe',
        'C:\Program Files\Redis\redis-cli.exe',
        'C:\Program Files\RedisLabs\Redis\redis-cli.exe',
        'C:\tools\redis\redis-cli.exe',
        'C:\redis\redis-cli.exe'
    )

    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $redisCliExe = $c
            break
        }
    }
}

if (-not $redisCliExe) {
    $cmd = Get-Command redis-cli -ErrorAction SilentlyContinue
    if ($cmd) {
        $redisCliExe = $cmd.Path
    }
}

if (-not $redisCliExe) {
    Write-Host "[CLX-PREP][WARN] redis-cli not found (PATH + common locations). Skipping INFO memory." -ForegroundColor Yellow
    Write-Host "[CLX-PREP] Optional: set env TNT_REDIS_CLI to a full redis-cli.exe path." -ForegroundColor DarkGray
    Write-Output "REDIS_INFO_OK=0 reason=redis-cli_missing"
    exit 0
}

Write-Host ("[CLX-PREP]   redis-cli={0}" -f $redisCliExe) -ForegroundColor DarkGray

# Run INFO memory. This is read-only.
$redisCli = $redisCliExe
$info = & $redisCli -h $RedisHost -p $RedisPort INFO memory 2>&1
$code = [int]$LASTEXITCODE

if ($code -ne 0) {
    Write-Host "[CLX-PREP][WARN] redis-cli returned exit code $code" -ForegroundColor Yellow
    $info | ForEach-Object { Write-Output $_ }
    Write-Output "REDIS_INFO_OK=0 reason=redis_cli_exit_$code"
    exit 0
}

# Print full output (small) and a couple helpful parsed lines.
$info | ForEach-Object { Write-Output $_ }

$used = ($info | Where-Object { $_ -like 'used_memory:*' } | Select-Object -First 1)
$peak = ($info | Where-Object { $_ -like 'used_memory_peak:*' } | Select-Object -First 1)
$frag = ($info | Where-Object { $_ -like 'mem_fragmentation_ratio:*' } | Select-Object -First 1)

Write-Host "\n[CLX-PREP] Redis memory summary" -ForegroundColor Cyan
if ($used) { Write-Host "  $used" -ForegroundColor DarkGray }
if ($peak) { Write-Host "  $peak" -ForegroundColor DarkGray }
if ($frag) { Write-Host "  $frag" -ForegroundColor DarkGray }

Write-Output "REDIS_INFO_OK=1"
