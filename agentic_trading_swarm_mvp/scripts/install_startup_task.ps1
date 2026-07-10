$ErrorActionPreference = "Stop"

$TaskName = "AgenticTradingRadar"
$StartScript = Resolve-Path (Join-Path $PSScriptRoot "start_radar_hidden.ps1")
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AgenticTradingRadar.lnk"
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
        -MultipleInstances IgnoreNew

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Starts the Agentic Trading Radar hidden supervisor at logon." `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Output "Installed scheduled task '$TaskName' to start the radar at logon."
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
    $shortcut.Description = "Starts the Agentic Trading Radar hidden supervisor."
    $shortcut.Save()

    Write-Output "Installed Startup shortcut: $ShortcutPath"
}
