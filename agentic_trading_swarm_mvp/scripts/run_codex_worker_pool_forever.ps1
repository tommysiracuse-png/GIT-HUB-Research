$ErrorActionPreference = "Continue"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$SrcDir = Join-Path $ProjectRoot "src"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$PoolEntryPoint = Join-Path $SrcDir "codex_worker_pool.py"
$LogPath = Join-Path $RunsDir "codex_worker_pool_forever.log"
$PidPath = Join-Path $RunsDir "codex_worker_pool_forever.pid"
$PidMetaPath = Join-Path $RunsDir "codex_worker_pool_forever.pid.json"
$HeartbeatPath = Join-Path $RunsDir "codex_worker_pool_heartbeat.json"
$MaxLogBytes = 10MB
$SuccessIntervalSeconds = 60
$InitialBackoffSeconds = 15
$MaxBackoffSeconds = 300

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

function Get-ExistingSupervisorProcess {
    if (-not (Test-Path $PidPath)) { return $null }
    try {
        $existingPid = [int](Get-Content $PidPath -ErrorAction Stop | Select-Object -First 1)
        if ($existingPid -le 0 -or $existingPid -eq $PID) { return $null }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
        if (-not $process) { return $null }
        $command = [string]$process.CommandLine
        if ($command -like "*run_codex_worker_pool_forever.ps1*" -and $command -like "*$ProjectRoot*") {
            return $process
        }
    }
    catch {}
    return $null
}

$existingSupervisor = Get-ExistingSupervisorProcess
if ($existingSupervisor) {
    exit 0
}

Set-Content -Path $PidPath -Value $PID -Encoding ASCII
$SupervisorStartedAt = (Get-Date).ToUniversalTime().ToString("o")
@{
    pid = $PID
    started_at_utc = $SupervisorStartedAt
    project_root = "$ProjectRoot"
    script = "run_codex_worker_pool_forever.ps1"
} | ConvertTo-Json | Set-Content -Path $PidMetaPath -Encoding UTF8

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:CODEX_WORKER_POOL_SUPERVISOR_PID = "$PID"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:RADAR_USE_LITELLM = "1"

$ProviderEnvNames = @(
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_API_KEY",
    "COHERE_API_KEY"
)
$LoadedProviderKeys = @()
foreach ($name in $ProviderEnvNames) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        $userValue = [Environment]::GetEnvironmentVariable($name, "User")
        if ($userValue) {
            [Environment]::SetEnvironmentVariable($name, $userValue, "Process")
        }
    }
    if ([Environment]::GetEnvironmentVariable($name, "Process")) {
        $LoadedProviderKeys += $name
    }
}

function Rotate-Log-IfNeeded {
    if (Test-Path $LogPath) {
        $item = Get-Item $LogPath
        if ($item.Length -gt $MaxLogBytes) {
            $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
            $archive = Join-Path $RunsDir "codex_worker_pool_forever_$stamp.log"
            Move-Item -Force -Path $LogPath -Destination $archive
            New-Item -ItemType File -Path $LogPath -Force | Out-Null
        }
    }
}

function Write-Log {
    param([string]$Message)
    Rotate-Log-IfNeeded
    $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    Add-Content -Path $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}

function Write-Heartbeat {
    param(
        [string]$Status,
        [int]$ConsecutiveFailures,
        [int]$BackoffSeconds,
        [string]$LastError = ""
    )
    @{
        supervisor_pid = $PID
        status = $Status
        started_at_utc = $SupervisorStartedAt
        last_updated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        project_root = "$ProjectRoot"
        log_path = "$LogPath"
        entry_point = "$PoolEntryPoint"
        consecutive_failures = $ConsecutiveFailures
        backoff_seconds = $BackoffSeconds
        last_error = $LastError
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $HeartbeatPath -Encoding UTF8
}

Write-Log "Codex worker-pool supervisor started. pid=$PID root=$ProjectRoot python=$PythonExe litellm=$($env:RADAR_USE_LITELLM) provider_keys=$($LoadedProviderKeys -join ',')"

$consecutiveFailures = 0
while ($true) {
    if (-not (Test-Path $PoolEntryPoint)) {
        $consecutiveFailures += 1
        $backoff = [Math]::Min($MaxBackoffSeconds, $InitialBackoffSeconds * [Math]::Pow(2, [Math]::Min($consecutiveFailures - 1, 4)))
        $message = "Codex worker-pool entry point is not available yet: $PoolEntryPoint"
        Write-Log $message
        Write-Heartbeat -Status "waiting_for_entry_point" -ConsecutiveFailures $consecutiveFailures -BackoffSeconds $backoff -LastError $message
        Start-Sleep -Seconds $backoff
        continue
    }

    $startedAt = (Get-Date).ToUniversalTime().ToString("o")
    @{
        supervisor_pid = $PID
        status = "running_iteration"
        started_at_utc = $SupervisorStartedAt
        iteration_started_at_utc = $startedAt
        project_root = "$ProjectRoot"
        log_path = "$LogPath"
        entry_point = "$PoolEntryPoint"
        consecutive_failures = $consecutiveFailures
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $HeartbeatPath -Encoding UTF8

    $exitCode = 1
    $failureMessage = ""
    Push-Location $ProjectRoot
    try {
        Write-Log "Starting one Codex worker-pool iteration."
        $output = & $PythonExe -B $PoolEntryPoint --iterations 1 --interval $SuccessIntervalSeconds 2>&1
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) {
            Add-Content -Path $LogPath -Value $line -Encoding UTF8
        }
        if ($exitCode -eq 0) {
            $consecutiveFailures = 0
            Write-Log "Codex worker-pool iteration exited with code 0."
        }
        else {
            $failureMessage = "Codex worker-pool iteration exited with code $exitCode."
            Write-Log $failureMessage
        }
    }
    catch {
        $failureMessage = "Codex worker-pool iteration failed: $($_.Exception.Message)"
        Write-Log $failureMessage
    }
    finally {
        Pop-Location
    }

    if ($exitCode -eq 0 -and -not $failureMessage) {
        Write-Heartbeat -Status "sleeping" -ConsecutiveFailures 0 -BackoffSeconds $SuccessIntervalSeconds
        Start-Sleep -Seconds $SuccessIntervalSeconds
        continue
    }

    $consecutiveFailures += 1
    $backoff = [Math]::Min($MaxBackoffSeconds, $InitialBackoffSeconds * [Math]::Pow(2, [Math]::Min($consecutiveFailures - 1, 4)))
    Write-Heartbeat -Status "backing_off" -ConsecutiveFailures $consecutiveFailures -BackoffSeconds $backoff -LastError $failureMessage
    Start-Sleep -Seconds $backoff
}
