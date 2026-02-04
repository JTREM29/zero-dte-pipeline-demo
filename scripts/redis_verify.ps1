param(
    [string]$RedisHost,
    [int]$RedisPort = 0
)

$envHost = [System.Environment]::GetEnvironmentVariable("TNT_REDIS_HOST")
$envPort = [System.Environment]::GetEnvironmentVariable("TNT_REDIS_PORT")

if (-not $RedisHost -or -not $RedisHost.Trim()) {
    $RedisHost = $envHost
}

if (-not $RedisHost -or -not $RedisHost.Trim()) {
    $RedisHost = "127.0.0.1"
} else {
    $RedisHost = $RedisHost.Trim()
}

try {
    if (-not $RedisPort -or $RedisPort -le 0) {
        $RedisPort = [int]$envPort
    }
} catch {
    $RedisPort = 0
}

if (-not $RedisPort -or $RedisPort -le 0) {
    $RedisPort = 6379
}

Write-Output "== Services (memurai*/redis*) =="
Get-Service -Name memurai*,redis* -ErrorAction SilentlyContinue |
    Select-Object Name,DisplayName,Status,StartType |
    Format-Table -AutoSize |
    Out-String |
    Write-Output

Write-Output "== TCP reachability =="
Write-Output ("Target: " + $RedisHost + "  Port: " + $RedisPort)
Test-NetConnection -ComputerName $RedisHost -Port $RedisPort |
    Select-Object ComputerName,RemotePort,TcpTestSucceeded |
    Format-Table -AutoSize |
    Out-String |
    Write-Output

Write-Output "== Local listeners (port $RedisPort) =="
Get-NetTCPConnection -LocalPort $RedisPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess |
    Format-Table -AutoSize |
    Out-String |
    Write-Output
