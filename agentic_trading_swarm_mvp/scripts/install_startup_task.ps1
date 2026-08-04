$ErrorActionPreference = "Stop"

$TaskName = "AgenticTradingSystem"
$LegacyTaskName = "AgenticTradingRadar"
$StartScript = Resolve-Path (Join-Path $PSScriptRoot "system_watchdog.ps1")
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AgenticTradingSystem.lnk"
$LegacyShortcutPath = Join-Path $StartupDir "AgenticTradingRadar.lnk"
$PowerShellPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

try {
    $Action = New-ScheduledTaskAction `
        -Execute $PowerShellPath `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -Force -ErrorAction SilentlyContinue $LegacyShortcutPath

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Keeps the Agentic Trading radar and evolution supervisors healthy for the current user." `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Output "Installed scheduled task '$TaskName' to keep radar and evolution running."
}
catch {
    Write-Output "Scheduled task install failed: $($_.Exception.Message)"
    Write-Output "Falling back to per-user Startup shortcut."

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $PowerShellPath
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
    $shortcut.WorkingDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Keeps the Agentic Trading radar and evolution supervisors healthy."
    $shortcut.Save()

    Write-Output "Installed Startup shortcut: $ShortcutPath"
}
