$ErrorActionPreference = "Continue"

$TaskName = "AgenticTradingRadar"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AgenticTradingRadar.lnk"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
if (Test-Path $ShortcutPath) {
    Remove-Item -Force $ShortcutPath
}

Write-Output "Removed scheduled task '$TaskName' and Startup shortcut if they existed."
