$ErrorActionPreference = "Continue"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$SrcDir = Join-Path $ProjectRoot "src"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$LogPath = Join-Path $RunsDir "evolution_worker_forever.log"
$PidPath = Join-Path $RunsDir "evolution_worker_forever.pid"
$PidMetaPath = Join-Path $RunsDir "evolution_worker_forever.pid.json"
$HeartbeatPath = Join-Path $RunsDir "evolution_worker_heartbeat.json"
$MaxLogBytes = 10MB
$IntervalSeconds = 300

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
Set-Content -Path $PidPath -Value $PID -Encoding ASCII
$SupervisorStartedAt = (Get-Date).ToUniversalTime().ToString("o")
@{ pid=$PID; started_at_utc=$SupervisorStartedAt; project_root="$ProjectRoot"; script="run_evolution_worker_forever.ps1" } | ConvertTo-Json | Set-Content -Path $PidMetaPath -Encoding UTF8
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:RADAR_USE_LITELLM = "1"

$ProviderEnvNames = @(
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
            $archive = Join-Path $RunsDir "evolution_worker_forever_$stamp.log"
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

Write-Log "Evolution worker supervisor started. pid=$PID root=$ProjectRoot python=$PythonExe litellm=$($env:RADAR_USE_LITELLM) provider_keys=$($LoadedProviderKeys -join ',') interval_seconds=$IntervalSeconds"

while ($true) {
    Rotate-Log-IfNeeded
    $startedAt = (Get-Date).ToUniversalTime().ToString("o")
    @{
        supervisor_pid = $PID
        status = "running_iteration"
        started_at_utc = $startedAt
        project_root = "$ProjectRoot"
        log_path = "$LogPath"
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $HeartbeatPath -Encoding UTF8

    Push-Location $ProjectRoot
    try {
        Write-Log "Starting one evolution worker iteration."
        $output = & $PythonExe -B (Join-Path $SrcDir "evolution_worker.py") --iterations 1 --interval $IntervalSeconds 2>&1
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) {
            Add-Content -Path $LogPath -Value $line -Encoding UTF8
        }
        Write-Log "Evolution worker iteration exited with code $exitCode."
    }
    catch {
        Write-Log "Evolution worker iteration failed: $($_.Exception.Message)"
    }
    finally {
        Pop-Location
    }

    @{
        supervisor_pid = $PID
        status = "sleeping"
        last_iteration_finished_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        project_root = "$ProjectRoot"
        log_path = "$LogPath"
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $HeartbeatPath -Encoding UTF8

    Start-Sleep -Seconds $IntervalSeconds
}
