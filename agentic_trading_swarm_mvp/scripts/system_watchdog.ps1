$ErrorActionPreference = "Continue"
$Starter = Resolve-Path (Join-Path $PSScriptRoot "start_system_hidden.ps1")
$RunsDir = Resolve-Path (Join-Path $PSScriptRoot "..\runs")
$Log = Join-Path $RunsDir "system_watchdog.log"
$PidPath = Join-Path $RunsDir "system_watchdog.pid"
$HeartbeatPath = Join-Path $RunsDir "system_watchdog_heartbeat.json"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

if (Test-Path $PidPath) {
    try {
        $existingPid = [int](Get-Content $PidPath | Select-Object -First 1)
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
        if ($existing -and [string]$existing.CommandLine -like "*system_watchdog.ps1*") {
            exit 0
        }
    }
    catch {}
}
Set-Content -Path $PidPath -Value $PID -Encoding ASCII

function Write-WatchdogHeartbeat {
    param([string]$Status, [string]$LastError = "")
    @{
        supervisor_pid = $PID
        status = $Status
        project_root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
        started_at_utc = $WatchdogStartedAt
        last_updated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        log_path = "$Log"
        last_error = $LastError
        managed_supervisors = @("radar", "evolution", "codex_worker_pool")
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $HeartbeatPath -Encoding UTF8
}

$WatchdogStartedAt = (Get-Date).ToUniversalTime().ToString("o")
Write-WatchdogHeartbeat -Status "started"

while ($true) {
    try {
        Write-WatchdogHeartbeat -Status "checking"
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Starter 2>&1
        $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
        foreach ($line in $output) { Add-Content -Path $Log -Value "[$stamp] $line" -Encoding UTF8 }
        Write-WatchdogHeartbeat -Status "sleeping"
    }
    catch {
        $message = "watchdog error: $($_.Exception.Message)"
        Add-Content -Path $Log -Value "[$((Get-Date).ToString('o'))] $message" -Encoding UTF8
        Write-WatchdogHeartbeat -Status "error" -LastError $message
    }
    Start-Sleep -Seconds 60
}
