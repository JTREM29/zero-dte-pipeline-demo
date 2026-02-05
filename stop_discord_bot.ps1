# Stops Discord bot instances for this repo (best-effort)
# Usage: .\stop_discord_bot.ps1

$ErrorActionPreference = 'Continue'

$repoRoot = (Resolve-Path -LiteralPath '.').Path

# NOTE: Get-Process does not expose CommandLine; use CIM so we can reliably
# detect processes launched from this repo and/or the bot module.
Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -match '^python') -and $_.CommandLine -and (
            ($_.CommandLine -like '*cli.discord_bot*') -or
            ($_.CommandLine -like '*delivery.discord_bot*') -or
            ($_.CommandLine -like '*delivery.discord_bot_head*')
        )
    } |
    ForEach-Object {
        Write-Output "Stopping PID=$($_.ProcessId) Name=$($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
