param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RadarRunnerPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run_bounded_paper_forever.ps1") -ErrorAction Stop).Path
$ResearchRunnerPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run_paid_research_supervisor.ps1") -ErrorAction Stop).Path
$ResolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).Path
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RadarPidPath = Join-Path $ProjectRoot "runs\bounded_paper_supervisor.pid.json"
$ResearchPidPath = Join-Path $ProjectRoot "runs\paid_research_supervisor.pid.json"
$RadarStartupTimeoutSeconds = 30
$ResearchStartupTimeoutSeconds = 30

# A real soak is a release action, never a feature-branch test. Fail before
# creating either supervisor unless the whole repository is clean and local
# main exactly matches its configured upstream ref.
$currentBranch = (& git -C $ProjectRoot branch --show-current 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $currentBranch -ne "main") {
    throw "Bounded rollout requires the checked-out main branch. Current branch: $currentBranch"
}
$worktreeChanges = @(& git -C $ProjectRoot status --porcelain 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to verify repository cleanliness; bounded rollout was not started."
}
if ($worktreeChanges.Count -ne 0) {
    throw "Bounded rollout requires a clean repository; bounded rollout was not started."
}
$upstreamBranch = (& git -C $ProjectRoot rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $upstreamBranch) {
    throw "Bounded rollout requires a configured upstream branch."
}
$upstreamDelta = (& git -C $ProjectRoot rev-list --left-right --count "HEAD...$upstreamBranch" 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $upstreamDelta -notmatch '^0\s+0$') {
    throw "Bounded rollout requires local main to exactly match its upstream; push or update it first."
}

function Stop-StartedProcessTree {
    param([int]$RootPid)
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $descendants = [System.Collections.Generic.List[int]]::new()
    $frontier = [System.Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($RootPid)
    while ($frontier.Count -gt 0) {
        $parent = $frontier.Dequeue()
        foreach ($row in $all) {
            if ([int]$row.ParentProcessId -eq $parent) {
                $childId = [int]$row.ProcessId
                $descendants.Add($childId)
                $frontier.Enqueue($childId)
            }
        }
    }
    $targets = @($descendants.ToArray())
    [array]::Reverse($targets)
    $targets += $RootPid
    foreach ($targetPid in $targets) {
        if ($null -eq (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) { continue }
        try {
            Stop-Process -Id $targetPid -Force -ErrorAction Stop
        }
        catch {
            if ($null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) { throw }
        }
    }
}

$radarArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$RadarRunnerPath`"",
    "-ConfigPath", "`"$ResolvedConfig`"",
    "-PythonExe", "`"$PythonExe`""
)
$radarLaunchedAt = [DateTimeOffset]::UtcNow
$radarProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $radarArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$radarReady = $false
$radarReadyDeadline = $radarLaunchedAt.AddSeconds($RadarStartupTimeoutSeconds)
while ([DateTimeOffset]::UtcNow -lt $radarReadyDeadline) {
    $radarProcess.Refresh()
    if ($radarProcess.HasExited) {
        throw "Bounded radar supervisor failed startup with exit code $($radarProcess.ExitCode); paid research was not started."
    }
    if (Test-Path -LiteralPath $RadarPidPath) {
        try {
            $pidRecord = Get-Content -Raw -LiteralPath $RadarPidPath | ConvertFrom-Json
            $recordedStart = [DateTimeOffset]::Parse([string]$pidRecord.started_at_utc)
            $recordedRunner = [System.IO.Path]::GetFullPath([string]$pidRecord.runner_path)
            $recordedConfig = [System.IO.Path]::GetFullPath([string]$pidRecord.config_path)
            if (
                [int]$pidRecord.pid -eq $radarProcess.Id -and
                $recordedStart -ge $radarLaunchedAt.AddSeconds(-1) -and
                $recordedRunner -eq $RadarRunnerPath -and
                $recordedConfig -eq $ResolvedConfig
            ) {
                $radarReady = $true
                break
            }
        }
        catch {
            # The supervisor writes the acknowledgement atomically enough for a
            # short parse retry; malformed/stale records never count as ready.
        }
    }
    Start-Sleep -Milliseconds 250
}
if (-not $radarReady) {
    $radarProcess.Refresh()
    if (-not $radarProcess.HasExited) {
        Stop-StartedProcessTree -RootPid $radarProcess.Id
    }
    throw "Bounded radar supervisor did not acknowledge a successful preflight; paid research was not started."
}

# Only after radar has passed preflight and acquired its singleton may the
# isolated daily research plane inherit provider credentials. Its one-shot
# child also enforces research phase, recent healthy radar evidence,
# once-per-UTC-day, campaign gates, and the shared between-cycle mutex.
$researchArguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$ResearchRunnerPath`"",
    "-ConfigPath", "`"$ResolvedConfig`"",
    "-PythonExe", "`"$PythonExe`""
)
$researchLaunchedAt = [DateTimeOffset]::UtcNow
$researchProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $researchArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$researchReady = $false
$researchReadyDeadline = $researchLaunchedAt.AddSeconds($ResearchStartupTimeoutSeconds)
while ([DateTimeOffset]::UtcNow -lt $researchReadyDeadline) {
    $researchProcess.Refresh()
    if ($researchProcess.HasExited) {
        Stop-StartedProcessTree -RootPid $radarProcess.Id
        throw "Paid research supervisor failed startup with exit code $($researchProcess.ExitCode); bounded radar was stopped."
    }
    if (Test-Path -LiteralPath $ResearchPidPath) {
        try {
            $researchPidRecord = Get-Content -Raw -LiteralPath $ResearchPidPath | ConvertFrom-Json
            $researchRecordedStart = [DateTimeOffset]::Parse([string]$researchPidRecord.started_at_utc)
            $researchRecordedRunner = [System.IO.Path]::GetFullPath([string]$researchPidRecord.runner_path)
            $researchRecordedConfig = [System.IO.Path]::GetFullPath([string]$researchPidRecord.config_path)
            if (
                [int]$researchPidRecord.pid -eq $researchProcess.Id -and
                $researchRecordedStart -ge $researchLaunchedAt.AddSeconds(-1) -and
                $researchRecordedRunner -eq $ResearchRunnerPath -and
                $researchRecordedConfig -eq $ResolvedConfig
            ) {
                $researchReady = $true
                break
            }
        }
        catch {
            # A stale or partially written record never acknowledges startup.
        }
    }
    Start-Sleep -Milliseconds 250
}
if (-not $researchReady) {
    $researchProcess.Refresh()
    if (-not $researchProcess.HasExited) {
        Stop-StartedProcessTree -RootPid $researchProcess.Id
    }
    $radarProcess.Refresh()
    if (-not $radarProcess.HasExited) {
        Stop-StartedProcessTree -RootPid $radarProcess.Id
    }
    throw "Paid research supervisor did not acknowledge startup; bounded radar was stopped."
}

foreach ($name in @(
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "AZURE_API_KEY",
    "COHERE_API_KEY", "CODEX_API_KEY", "RADAR_RESEARCH_MODEL_OVERRIDE"
)) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
$env:RADAR_MODEL_CREDENTIAL_LOCK = "1"
$env:RADAR_MODELS_DISABLED = "1"
$env:RADAR_USE_LITELLM = "0"
Write-Output "Started bounded paper supervisor pid=$($radarProcess.Id) after preflight acknowledgement and paid research supervisor pid=$($researchProcess.Id) config=$ResolvedConfig"
