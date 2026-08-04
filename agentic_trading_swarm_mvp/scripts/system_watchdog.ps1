$ErrorActionPreference = "Continue"
$Starter = Resolve-Path (Join-Path $PSScriptRoot "start_system_hidden.ps1")
$RunsDir = Resolve-Path (Join-Path $PSScriptRoot "..\runs")
$Log = Join-Path $RunsDir "system_watchdog.log"
$PidPath = Join-Path $RunsDir "system_watchdog.pid"
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

while ($true) {
    try {
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Starter 2>&1
        $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
        foreach ($line in $output) { Add-Content -Path $Log -Value "[$stamp] $line" -Encoding UTF8 }
    }
    catch {
        Add-Content -Path $Log -Value "[$((Get-Date).ToString('o'))] watchdog error: $($_.Exception.Message)" -Encoding UTF8
    }
    Start-Sleep -Seconds 60
}
