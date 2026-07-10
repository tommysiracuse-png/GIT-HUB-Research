$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$PidPath = Join-Path $RunsDir "radar_forever.pid"
$Runner = Join-Path $PSScriptRoot "run_radar_forever.ps1"

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

if (Test-Path $PidPath) {
    $existingPid = (Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
        Write-Output "Radar supervisor already running with PID $existingPid"
        exit 0
    }
}

$process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Runner`"") `
    -WindowStyle Hidden `
    -PassThru

Set-Content -Path $PidPath -Value $process.Id -Encoding ASCII
Write-Output "Started radar supervisor with PID $($process.Id)"
