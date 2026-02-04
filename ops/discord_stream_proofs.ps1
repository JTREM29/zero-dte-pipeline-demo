param(
  [string]$BotLog = ".\\logs\\bot_live.log",
  [string]$SlashLog = ".\\logs\\slash_live.log",
  [int]$Tail = 800
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "=== TNT Discord Streams Proofs ===" -ForegroundColor Cyan
Write-Host "BotLog:   $BotLog"
Write-Host "SlashLog: $SlashLog"
Write-Host ""

if (!(Test-Path $BotLog)) { Write-Host "[WARN] Bot log not found: $BotLog" -ForegroundColor Yellow }
if (!(Test-Path $SlashLog)) { Write-Host "[WARN] Slash log not found: $SlashLog" -ForegroundColor Yellow }

Write-Host "`n--- [TNT][CONFIG] routing banner (bot) ---" -ForegroundColor Green
if (Test-Path $BotLog) {
  Get-Content -Tail $Tail $BotLog |
    Select-String "\[TNT\]\[CONFIG\] channels=|\[TNT\]\[CONFIG\] outlook_kind=|\[TNT\]\[CONFIG\] earnings_provider=|\[TNT\]\[CONFIG\] weekly_outlook_" |
    Select-Object -Last 80 |
    ForEach-Object { $_.Line }
}

Write-Host "`n--- Autopost proofs: OUTLOOK / WEEKLY_OUTLOOK / EARNINGS / EARNINGS_RESULTS (bot) ---" -ForegroundColor Green
if (Test-Path $BotLog) {
  Get-Content -Tail $Tail $BotLog |
    Select-String "\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK|EARNINGS|EARNINGS_RESULTS)\]( posting| deduped)|\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK|EARNINGS|EARNINGS_RESULTS)\]\[PROOF\]" |
    Select-Object -Last 160 |
    ForEach-Object { $_.Line }
}

Write-Host "`n--- Alerts delivery proofs: POP/BUNDLE/POSTED/WARN (slash) ---" -ForegroundColor Green
if (Test-Path $SlashLog) {
  Get-Content -Tail $Tail $SlashLog |
    Select-String "\[TNT\]\[ALERTS\]\[DELIVERY\]\[(POP|BUNDLE|POSTED|WARN)\]" |
    Select-Object -Last 120 |
    ForEach-Object { $_.Line }
}

Write-Host "`nDone." -ForegroundColor Cyan
