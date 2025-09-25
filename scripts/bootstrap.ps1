# --- bootstrap.ps1: create a fresh SPX 0DTE scaffold ---
$ErrorActionPreference = "Stop"

# 1) Folders
$dirs = @(
    "src",
    "src/datafeeds",
    "src/strategies",
    "src/utils",
    "scripts",
    "tests",
    ".vscode",
    "logs",
    "data"
)
$dirs | ForEach-Object { New-Item -ItemType Directory -Force -Path $_ | Out-Null }

# 2) .gitignore (only writes if not present)
if (-not (Test-Path .gitignore)) {
    @'
# Python
.venv/
__pycache__/
*.pyc
*.pyo
*.pyd
*.egg-info/
.build/
dist/
.cache/
logs/
.data/
.env
# VS Code
.vscode/*
!.vscode/settings.json
# OS
.DS_Store
Thumbs.db
'@ | Set-Content .gitignore -Encoding UTF8
}

Write-Host "Bootstrap completed." -ForegroundColor Green
