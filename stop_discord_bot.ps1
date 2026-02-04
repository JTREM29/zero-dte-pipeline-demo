# Stops Discord bot instances for this repo (best-effort)
# Usage: .\stop_discord_bot.ps1

$ErrorActionPreference = 'Continue'

$repoRoot = (Resolve-Path -LiteralPath '.').Path

# Stop any supervised runners first (they can auto-restart python processes).
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and ($_.Name -match '^(powershell|pwsh)') -and ($_.CommandLine -like '*run_discord_supervised.ps1*')
    } |
    ForEach-Object {
        Write-Output "Stopping supervisor PID=$($_.ProcessId) Name=$($_.Name)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

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
