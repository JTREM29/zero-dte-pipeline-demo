# scripts/harden_memurai_recovery.ps1
# Run in Admin PowerShell

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$svcName = "Memurai"

$svc = Get-Service -Name $svcName -ErrorAction Stop
Write-Host "[memurai] service found: $($svc.Name) status=$($svc.Status)"

# Set recovery actions: restart after 60s, reset after 1 day
# Note: sc.exe requires spaces after '='
sc.exe failure $svcName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Host

# Optional: ensure it starts automatically
sc.exe config $svcName start= auto | Out-Host

Write-Host "[memurai] recovery policy applied."

# Show config (best-effort)
sc.exe qfailure $svcName | Out-Host
sc.exe qc $svcName | Out-Host

Write-Host "[memurai] done."
