$ErrorActionPreference = "Continue"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$PidPath = Join-Path $RunsDir "radar_forever.pid"

if (-not (Test-Path $PidPath)) {
    Write-Output "No PID file found. Radar does not appear to be running."
    exit 0
}

$pidValue = Get-Content $PidPath | Select-Object -First 1
if (-not $pidValue) {
    Write-Output "PID file is empty."
    Remove-Item -Force $PidPath
    exit 0
}

$process = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Output "PID $pidValue is not running. Removing stale PID file."
    Remove-Item -Force $PidPath
    exit 0
}

$children = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq [int]$pidValue }
foreach ($child in $children) {
    Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
}
Stop-Process -Id ([int]$pidValue) -Force -ErrorAction SilentlyContinue
Remove-Item -Force $PidPath
Write-Output "Stopped radar supervisor PID $pidValue"
