# stop_live.ps1 — stop TNT supervisor windows + python processes for this repo
Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$escaped = [regex]::Escape($Repo)

Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -match $escaped -and (
      $_.CommandLine -match "powershell\.exe" -or
      $_.CommandLine -match "python3\.13\.exe" -or
      $_.CommandLine -match "python\.exe" -or
      $_.CommandLine -match "cmd\.exe"
    )
  } |
  ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
  }

Write-Host "[OK] Stop executed."
