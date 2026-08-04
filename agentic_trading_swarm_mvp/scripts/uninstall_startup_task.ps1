$ErrorActionPreference = "Continue"

$TaskName = "AgenticTradingSystem"
$LegacyTaskName = "AgenticTradingRadar"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AgenticTradingSystem.lnk"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $StartupDir "AgenticTradingRadar.lnk")
if (Test-Path $ShortcutPath) {
    Remove-Item -Force $ShortcutPath
}

Write-Output "Removed scheduled task '$TaskName' and Startup shortcut if they existed."
