param(
  [string]$Prefix = "E2E_GREEN",
  [string]$Message = "Ask-TNT + Macro Pulse + Futures + Worker all green",
  [switch]$AllowDirty,
  [switch]$NoPush
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function _Fail {
  param([Parameter(Mandatory = $true)][string]$Msg)
  Write-Host "[FAIL] $Msg" -ForegroundColor Red
  exit 1
}

try {
  $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..") | Select-Object -First 1 -ExpandProperty Path)
  Set-Location $repoRoot
} catch {
  _Fail "Unable to resolve repo root."
}

try {
  $inside = (git rev-parse --is-inside-work-tree 2>$null)
  if (-not $inside -or ($inside.Trim() -ne "true")) {
    _Fail "Not a git repository: $repoRoot"
  }
} catch {
  _Fail "git not available or not a repository."
}

if (-not $AllowDirty) {
  $dirty = @()
  try { $dirty = @(git status --porcelain 2>$null) } catch { $dirty = @() }
  if ($dirty.Count -gt 0) {
    Write-Host "[WARN] repo has uncommitted changes:" -ForegroundColor Yellow
    $dirty | Select-Object -First 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    if ($dirty.Count -gt 25) { Write-Host "  (showing first 25 of $($dirty.Count))" -ForegroundColor DarkGray }
    _Fail "Refusing to tag a dirty working tree. Commit/stash, or re-run with -AllowDirty."
  }
}

$ts = Get-Date -Format "yyyyMMdd_HHmm"
$baseTag = "${Prefix}_${ts}"

$tag = $baseTag
for ($i = 1; $i -le 20; $i++) {
  $exists = $null
  try { $exists = (git tag -l $tag 2>$null) } catch { $exists = $null }
  if (-not $exists) { break }
  $tag = "${baseTag}_" + $i.ToString("00")
}

Write-Host "[OPS] tagging $tag" -ForegroundColor Cyan

try {
  git tag -a $tag -m $Message
} catch {
  _Fail "Failed to create annotated tag '$tag'."
}

if ($NoPush) {
  Write-Host "[OK] tag created locally (no push): $tag" -ForegroundColor Green
  exit 0
}

$origin = $null
try { $origin = (git remote get-url origin 2>$null) } catch { $origin = $null }
if (-not $origin) {
  _Fail "Remote 'origin' not configured. Tag created locally as '$tag'. Push manually or re-run with -NoPush."
}

try {
  git push origin $tag
} catch {
  _Fail "Failed to push tag '$tag' to origin."
}

Write-Host "[OK] pushed tag: $tag" -ForegroundColor Green
exit 0
