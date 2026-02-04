param(
    [string]$RedisIp = '127.0.0.1',
    [int]$Port = 6379,
    [string]$ServiceName = 'Memurai',
    [string]$ExePath = 'C:\Program Files\Memurai\memurai.exe',
    [string]$ConfigPath = 'C:\Program Files\Memurai\memurai.conf',
    [int]$StartupWaitSec = 5,
    [switch]$NoExeFallback
)

$ErrorActionPreference = 'Stop'

function Test-IsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Test-RedisListening {
    param([string]$H, [int]$P)
    try {
        return [bool]((Test-NetConnection $H -Port $P -WarningAction SilentlyContinue).TcpTestSucceeded)
    } catch {
        return $false
    }
}

function Write-Status {
    param([string]$Msg)
    $ts = (Get-Date).ToString('s')
    Write-Host "[$ts] $Msg"
}

if (Test-RedisListening -H $RedisIp -P $Port) {
    Write-Status "[OK] Redis already listening on $RedisIp`:$Port"
    exit 0
}

$isAdmin = Test-IsAdmin
if (-not $isAdmin) {
    Write-Status "[WARN] Not running elevated (Administrator). Starting Memurai as a Windows service typically requires admin rights."
    Write-Status "[HINT] Re-run from an elevated PowerShell (Run as Administrator)."
    Write-Status "[HINT] Verify admin group: whoami /groups | findstr /i BUILTIN\\Administrators"
}

# Try service start first (recommended). Requires elevation.
try {
    $svc = Get-Service -Name $ServiceName -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Write-Status "[INFO] Starting service '$ServiceName'..."
        Start-Service -Name $ServiceName
        Start-Sleep -Seconds 2
    }
} catch {
    Write-Status "[WARN] Service start attempt failed for '$ServiceName': $($_.Exception.Message)"
}

if (Test-RedisListening -H $RedisIp -P $Port) {
    Write-Status "[OK] Redis listening after service start on $RedisIp`:$Port"
    exit 0
}

if ($NoExeFallback) {
    Write-Status "[ERROR] Redis not listening and -NoExeFallback set"
    exit 2
}

# Fallback: launch Memurai as a process (works without elevation).
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Status "[ERROR] Memurai exe not found: $ExePath"
    exit 3
}

$procParams = @()
if (Test-Path -LiteralPath $ConfigPath) {
    $procParams += @($ConfigPath)
}

Write-Status "[INFO] Launching Memurai process fallback: $ExePath $($procParams -join ' ')"
try {
    Start-Process -FilePath $ExePath -ArgumentList $procParams -WindowStyle Hidden | Out-Null
} catch {
    Write-Status "[ERROR] Start-Process failed: $($_.Exception.Message)"
    exit 4
}

Start-Sleep -Seconds ([Math]::Max(1, $StartupWaitSec))

if (Test-RedisListening -H $RedisIp -P $Port) {
    Write-Status "[OK] Redis listening after process launch on $RedisIp`:$Port"
    exit 0
}

Write-Status "[ERROR] Redis still not listening on $RedisIp`:$Port"
exit 5
