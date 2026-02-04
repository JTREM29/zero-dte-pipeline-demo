param(
  [string[]]$Symbols = @('SPY','QQQ','IWM'),
  [int]$Top = 18,
  [int]$Dpi = 130,
  [int]$TimeoutSec = 60
)

$env:PYTHONPATH = "C:\Users\jttre\OneDrive\Desktop\ZeroDTE-pipeline"
$env:TNT_SMB_JOBS_DIR    = "\\\\TNT2\\tnt_jobs"
$env:TNT_SMB_RESULTS_DIR = "\\\\TNT2\\tnt_results"

Write-Host "Prewarm OI via SMB -> Jobs=$env:TNT_SMB_JOBS_DIR Results=$env:TNT_SMB_RESULTS_DIR"
Write-Host ("Symbols: " + ($Symbols -join ", ") + " | top=$Top dpi=$Dpi timeout=$TimeoutSec s")

foreach ($s in $Symbols) {
  Write-Host ""
  Write-Host ("=== PREWARM " + $s + " ===")
  py -u "C:\Users\jttre\OneDrive\Desktop\ZeroDTE-pipeline\scripts\enqueue_and_wait_smb.py" "$s" "render_oi" "$Top" "$Dpi" "$TimeoutSec"
}

Write-Host ""
Write-Host "Done."
