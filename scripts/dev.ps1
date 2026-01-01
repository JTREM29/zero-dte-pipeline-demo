param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("test-smoke", "test")]
    [string]$Task
)

$env:PYTHONUTF8 = "1"

switch ($Task) {
    "test-smoke" {
        Write-Host "[dev] Running smoke regression..."
        pytest -q tests/test_render_posts_smoke.py
    }
    "test" {
        Write-Host "[dev] Running full pytest suite..."
        pytest -q
    }
    default {
        Write-Error "Unknown task $Task"
        exit 1
    }
}
