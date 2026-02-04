param(
    [string]$Url = "http://192.168.1.145:8787/healthz",
    [int]$TimeoutSec = 2
)

try {
    $obj = Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec
    Write-Output ("OK {0}" -f $Url)
    if ($null -ne $obj) {
        try {
            $obj | ConvertTo-Json -Compress | Write-Output
        }
        catch {
            $obj | Out-String | Write-Output
        }
    }
    exit 0
}
catch {
    Write-Output ("FAIL {0}" -f $_.Exception.Message)
    exit 1
}
