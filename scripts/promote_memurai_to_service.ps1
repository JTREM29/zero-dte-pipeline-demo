# scripts/promote_memurai_to_service.ps1
# Promotes a console-run Memurai into a Windows Service configuration.
# Run in an elevated PowerShell (Run as Administrator).

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Get-ListenerPid([int]$port) {
    $c = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if (-not $c -or $c.Count -eq 0) { return $null }
    return [int]$c[0].OwningProcess
}

$svcName = "Memurai"
$port = 6379

if (-not (Test-IsAdmin)) {
    Write-Host "[memurai][ERROR] Not running elevated. Re-run in Admin PowerShell." -ForegroundColor Red
    Write-Host "[memurai][HINT] Start Menu -> PowerShell -> Right-click -> Run as administrator" -ForegroundColor Yellow
    exit 5
}

$svc = Get-Service -Name $svcName -ErrorAction Stop
Write-Host "[memurai] service found: $($svc.Name) status=$($svc.Status)" -ForegroundColor Cyan

# If 6379 is already bound, determine the owning PID.
$listenerPid = Get-ListenerPid -port $port
if ($listenerPid) {
    $svcWmi = Get-CimInstance Win32_Service -Filter "Name='$svcName'" -ErrorAction SilentlyContinue
    $svcPid = if ($svcWmi) { [int]$svcWmi.ProcessId } else { 0 }

    if ($svc.Status -ne 'Running') {
        # Service isn't running but port is bound. If it's Memurai, stop it; otherwise abort.
        $p = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
        $pName = if ($p) { $p.ProcessName } else { "<unknown>" }

        if ($pName -match '^memurai$') {
            Write-Host "[memurai] Port $port is held by memurai PID=$listenerPid (console mode). Stopping it to free the port..." -ForegroundColor Yellow
            Stop-Process -Id $listenerPid -Force
            Start-Sleep -Seconds 1
        } else {
            Write-Host "[memurai][ERROR] Port $port is already listening by PID=$listenerPid ($pName)." -ForegroundColor Red
            Write-Host "[memurai][HINT] Stop that process or change TNT_REDIS_PORT, then re-run." -ForegroundColor Yellow
            exit 6
        }
    } elseif ($svcPid -and $listenerPid -ne $svcPid) {
        Write-Host "[memurai][WARN] Service is running but a different PID is listening on $port (listenerPid=$listenerPid, servicePid=$svcPid)." -ForegroundColor Yellow
    }
}

# Ensure auto start and recovery policies.
Write-Host "[memurai] configuring service startup + recovery..." -ForegroundColor Cyan
sc.exe config $svcName start= auto | Out-Host
sc.exe failure $svcName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Host

# Start service.
Write-Host "[memurai] starting service..." -ForegroundColor Cyan
Start-Service -Name $svcName
Start-Sleep -Seconds 2

$svc = Get-Service -Name $svcName
Write-Host "[memurai] service status now: $($svc.Status)" -ForegroundColor Cyan

$tncOk = [bool](Test-NetConnection 127.0.0.1 -Port $port -InformationLevel Quiet)
Write-Host "[memurai] redis listener 127.0.0.1:$port = $tncOk" -ForegroundColor Cyan

# Best-effort ping.
$pingOk = $false
try {
    if (Get-Command redis-cli -ErrorAction SilentlyContinue) {
        $pong = (redis-cli -h 127.0.0.1 -p $port ping 2>$null)
        if ($pong -match 'PONG') { $pingOk = $true }
    } else {
        $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
        $venvPy = Join-Path $repoRoot '.venv\Scripts\python.exe'
        if (Test-Path $venvPy) {
            $out = & $venvPy -c "import redis; r=redis.Redis(host='127.0.0.1',port=$port); print(r.ping())" 2>$null
            if ($out -match 'True') { $pingOk = $true }
        }
    }
} catch {
    $pingOk = $false
}

Write-Host "[memurai] redis ping = $pingOk" -ForegroundColor Cyan

Write-Host "[memurai] done." -ForegroundColor Green
