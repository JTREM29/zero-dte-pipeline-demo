param(
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $repoRoot
Set-Location $repoRoot

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    if (Test-Path -LiteralPath .\.venv\Scripts\python.exe) {
        $PythonExe = (Resolve-Path .\.venv\Scripts\python.exe).Path
    }
    else {
        $PythonExe = 'python'
    }
}

# Load dotenv so Task Scheduler runs get TNT_WORKER_URL etc.
function Import-DotEnvFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Override
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_
        if ([string]::IsNullOrWhiteSpace($line)) { return }
        $trim = $line.Trim()
        if ($trim.StartsWith('#')) { return }

        $eq = $trim.IndexOf('=')
        if ($eq -lt 1) { return }

        $key = $trim.Substring(0, $eq).Trim()
        $value = $trim.Substring($eq + 1)

        if ([string]::IsNullOrWhiteSpace($key)) { return }

        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }

        $current = [Environment]::GetEnvironmentVariable($key)
        if ($Override -or [string]::IsNullOrWhiteSpace($current)) {
            Set-Item -Path "Env:$key" -Value $value
        }
    }
}

Import-DotEnvFile -Path (Join-Path $repoRoot '.env')
Import-DotEnvFile -Path (Join-Path $repoRoot '.env.local') -Override

New-Item -ItemType Directory -Force -Path .\logs | Out-Null

$log = Join-Path $repoRoot 'logs\parity_oi_iv_stdout.log'
$err = Join-Path $repoRoot 'logs\parity_oi_iv_stderr.log'

# Run and capture output to log files.
& $PythonExe -u .\scripts\parity_check_oi_iv.py 1> $log 2> $err
exit ([int]$LASTEXITCODE)
