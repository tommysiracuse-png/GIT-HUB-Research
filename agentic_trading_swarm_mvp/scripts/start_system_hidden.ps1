param(
    [int]$RadarStaleMinutes = 30,
    [int]$EvolutionStaleMinutes = 45
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunsDir = Join-Path $ProjectRoot "runs"
New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try { return Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
}

function Get-PidValue {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return 0 }
    try { return [int](Get-Content $Path -ErrorAction Stop | Select-Object -First 1) }
    catch { return 0 }
}

function Test-Supervisor {
    param(
        [string]$Name,
        [string]$ExpectedScript,
        [string]$PidPath,
        [string]$MetaPath,
        [string]$HeartbeatPath,
        [int]$StaleMinutes
    )
    $pidValue = Get-PidValue $PidPath
    if ($pidValue -le 0) { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    $command = [string]$process.CommandLine
    if ($command -notlike "*$ExpectedScript*" -or $command -notlike "*$ProjectRoot*") { return $false }
    $meta = Read-JsonFile $MetaPath
    if (-not $meta -or [int]$meta.pid -ne $pidValue -or [string]$meta.project_root -ne $ProjectRoot) { return $false }
    try {
        $recordedStart = [DateTimeOffset]::Parse([string]$meta.started_at_utc).UtcDateTime
        if ($process.CreationDate -is [DateTime]) {
            $actualStart = $process.CreationDate.ToUniversalTime()
        }
        else {
            $actualStart = [Management.ManagementDateTimeConverter]::ToDateTime([string]$process.CreationDate).ToUniversalTime()
        }
        if ([Math]::Abs(($actualStart - $recordedStart).TotalMinutes) -gt 2) { return $false }
    }
    catch { return $false }
    $heartbeat = Read-JsonFile $HeartbeatPath
    if (-not $heartbeat -or [int]$heartbeat.supervisor_pid -ne $pidValue -or [string]$heartbeat.project_root -ne $ProjectRoot) { return $false }
    $stamp = $heartbeat.last_iteration_finished_at_utc
    if (-not $stamp) { $stamp = $heartbeat.started_at_utc }
    try {
        if (((Get-Date).ToUniversalTime() - [DateTimeOffset]::Parse([string]$stamp).UtcDateTime).TotalMinutes -gt $StaleMinutes) {
            return $false
        }
    }
    catch { return $false }
    return $true
}

function Start-Supervisor {
    param([string]$Name, [string]$Runner, [string]$PidPath)
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Runner`"") `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path $PidPath -Value $process.Id -Encoding ASCII
    Write-Output "Started $Name supervisor with PID $($process.Id)"
}

$definitions = @(
    @{
        Name = "radar"
        Script = "run_radar_forever.ps1"
        Pid = Join-Path $RunsDir "radar_forever.pid"
        Meta = Join-Path $RunsDir "radar_forever.pid.json"
        Heartbeat = Join-Path $RunsDir "radar_heartbeat.json"
        Stale = $RadarStaleMinutes
    },
    @{
        Name = "evolution"
        Script = "run_evolution_worker_forever.ps1"
        Pid = Join-Path $RunsDir "evolution_worker_forever.pid"
        Meta = Join-Path $RunsDir "evolution_worker_forever.pid.json"
        Heartbeat = Join-Path $RunsDir "evolution_worker_heartbeat.json"
        Stale = $EvolutionStaleMinutes
    }
)

foreach ($definition in $definitions) {
    $runner = (Resolve-Path (Join-Path $PSScriptRoot $definition.Script)).Path
    $healthy = Test-Supervisor `
        -Name $definition.Name `
        -ExpectedScript $definition.Script `
        -PidPath $definition.Pid `
        -MetaPath $definition.Meta `
        -HeartbeatPath $definition.Heartbeat `
        -StaleMinutes $definition.Stale
    if ($healthy) {
        Write-Output "$($definition.Name) supervisor is healthy."
        continue
    }
    $oldPid = Get-PidValue $definition.Pid
    if ($oldPid -gt 0) {
        $oldProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue
        if ($oldProcess -and [string]$oldProcess.CommandLine -like "*$($definition.Script)*" -and [string]$oldProcess.CommandLine -like "*$ProjectRoot*") {
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
    }
    Start-Supervisor -Name $definition.Name -Runner $runner -PidPath $definition.Pid
}
