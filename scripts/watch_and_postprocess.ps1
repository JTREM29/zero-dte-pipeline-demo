param(
    [Parameter(Mandatory = $true)][string]$Stem,
    [string]$LogsDir = "logs",
    [int]$TimeoutSeconds = 900,
    [string]$Python = ".\.venv\Scripts\python.exe"
)

# Waits for live run artifacts and post-processes: summary JSON, PD plot, and causal probe (if sentiments exist)
$bars = Join-Path $LogsDir ("${Stem}_bars.csv")
$preds = Join-Path $LogsDir ("${Stem}_preds.csv")
$summary = Join-Path $LogsDir ("${Stem}_summary.json")

Write-Host "Watching for bars=$bars and preds=$preds for up to $TimeoutSeconds seconds..."
$start = Get-Date
while (-not (Test-Path $bars) -and ((Get-Date) - $start).TotalSeconds -lt $TimeoutSeconds) {
    Start-Sleep -Seconds 2
}
if (-not (Test-Path $bars)) {
    Write-Warning "Bars file not found within timeout: $bars"
    exit 1
}
# Give the run a few seconds to finish writing preds
Start-Sleep -Seconds 3

# Summarize
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python "scripts/summarize_live.py" $bars $preds --out $summary --join asof --tolerance-seconds 5

# Notify if predictions exist, summary shows potential signals, or order/exec files are present
try {
    $shouldNotify = $false
    $tradeMsg = "Signal(s) detected for $Stem. Check $summary"
    if (Test-Path $preds) {
        # quick check for non-header content
        $lineCount = (Get-Content -Path $preds -TotalCount 3 | Measure-Object -Line).Lines
        if ($lineCount -ge 2) { $shouldNotify = $true }
    }
    if (-not $shouldNotify -and (Test-Path $summary)) {
        try {
            $j = Get-Content -Raw -Path $summary | ConvertFrom-Json
            if ($j -and ($j.has_preds -eq $true -or $j.rows_preds -gt 0)) { $shouldNotify = $true }
        }
        catch {}
    }
    # Check for trade-like artifacts
    try {
        $stemPrefix = Join-Path $LogsDir $Stem
        $tradeFiles = @()
        $tradeFiles += Get-ChildItem -LiteralPath $LogsDir -File -Filter ("${Stem}_orders*.csv") -ErrorAction SilentlyContinue
        $tradeFiles += Get-ChildItem -LiteralPath $LogsDir -File -Filter ("${Stem}_executions*.csv") -ErrorAction SilentlyContinue
        $tradeFiles += Get-ChildItem -LiteralPath $LogsDir -File -Filter ("${Stem}_fills*.csv") -ErrorAction SilentlyContinue
        if ($tradeFiles -and $tradeFiles.Count -gt 0) {
            $shouldNotify = $true
            $tradeMsg = "Trade activity detected for ${Stem}: " + ($tradeFiles | Select-Object -ExpandProperty Name -First 1)
        }
    }
    catch {}
    if ($shouldNotify) {
        $notify = Join-Path $PSScriptRoot "notify_trade.ps1"
        if (Test-Path -LiteralPath $notify) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $notify -Message $tradeMsg -Title "ZeroDTE Trade Alert"
        }
    }
}
catch { Write-Host "notify skipped: $_" }

# If preds exist, try partial dependence for hybrid on sentiment if present, else r
if (Test-Path $preds) {
    $feature = "sentiment"
    # Quick heuristic: if preds has a sentiment column
    try {
        $hasSent = Select-String -Path $preds -Pattern "sentiment" -SimpleMatch -Quiet
    }
    catch { $hasSent = $false }
    if (-not $hasSent) { $feature = "r" }
    & $Python -m src.cli interpret_hybrid_live $bars --preds-csv $preds --feature $feature --window 64 --horizon 1 --out-stem ("pd_" + $Stem)
}

# Optional causal probe
try {
    & $Python -m src.cli causal_probe $bars --preds-csv $preds --out-stem ("causal_" + $Stem)
}
catch { Write-Host "causal_probe skipped: $_" }

# Live agent commentary (if OpenAI key is available)
try {
    if ($Env:OPENAI_API_KEY) {
        Write-Host "Running live agent commentary..."
        $prompt = "Summarize this live run in 3-6 sentences for a trading desk. Consider hit-rate, correlation, bias, and any anomalies. If rows are zero, state likely causes (hours/entitlement). Summary file: $summary"
        # Prefer repo venv if the provided python doesn't exist
        if (-not (Test-Path $Python)) { $Python = "python" }
        $agentOut = & $Python -m src.cli agent_demo --backend auto --prompt $prompt 2>$null
        $agentPath = Join-Path $LogsDir ("${Stem}_agent.json")
        if ($agentOut) { Set-Content -LiteralPath $agentPath -Value $agentOut -Encoding UTF8 }
        Write-Host "Agent output -> $agentPath"
    }
}
catch { Write-Host "agent_demo skipped: $_" }

Write-Host "Post-processing complete. Summary at $summary"
