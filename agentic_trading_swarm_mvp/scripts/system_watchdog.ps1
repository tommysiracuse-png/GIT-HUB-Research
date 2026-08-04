$ErrorActionPreference = "Continue"
$Starter = Resolve-Path (Join-Path $PSScriptRoot "start_system_hidden.ps1")
$Log = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\runs")) "system_watchdog.log"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

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
