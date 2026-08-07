param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PythonExe = "python",
    [int]$MaxCycles = 0
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RunnerPath = [System.IO.Path]::GetFullPath($PSCommandPath)
$RadarEntryPoint = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "src\radar_loop.py"))
$PreflightEntryPoint = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "src\recovery_preflight.py"))
$SupervisorEventEntryPoint = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "src\campaign_supervisor_event.py"))
$ResolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).Path
$RunsDir = Join-Path $ProjectRoot "runs"
$HeartbeatPath = Join-Path $RunsDir "bounded_paper_heartbeat.json"
$PidPath = Join-Path $RunsDir "bounded_paper_supervisor.pid.json"
$StdoutPath = Join-Path $RunsDir "bounded_paper_cycle.stdout.log"
$StderrPath = Join-Path $RunsDir "bounded_paper_cycle.stderr.log"
$CadenceSeconds = 900
$TimeoutSeconds = 720
$HeartbeatSeconds = 15
$ForbiddenWorkspaceProcessMarkers = @(
    "system_watchdog.ps1",
    "start_system_hidden.ps1",
    "run_radar_forever.ps1",
    "run_evolution_worker_forever.ps1",
    "evolution_worker.py",
    "run_codex_worker_pool_forever.ps1",
    "codex_worker_pool.py",
    "research_worker.py",
    "run_paid_research_once.ps1",
    "paid_research_once.py",
    "adapter_implementation_owner.py",
    "market_activation_owner.py",
    "strategy_implementation_owner.py",
    "autonomous_builder.py",
    "code_evolution.py"
)

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

function Get-IdentityHash {
    # Every supported config in this workspace writes to the same radar.sqlite.
    # Key locks to that database identity, not to a config filename, so copied
    # or reordered versions of the same config cannot bypass mutual exclusion.
    $material = $ProjectRoot.ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material)))).Replace("-", "").Substring(0, 24)
    }
    finally {
        $sha.Dispose()
    }
}

function Test-ExactExistingProcess {
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ([int]$process.ProcessId -eq $PID) { continue }
        $command = [string]$process.CommandLine
        if (-not $command) { continue }
        $isThisSupervisor = $command.IndexOf($RunnerPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $isLegacySupervisor = $command.IndexOf("run_radar_forever.ps1", [StringComparison]::OrdinalIgnoreCase) -ge 0
        $isExactRadar = $command.IndexOf($RadarEntryPoint, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $isForbiddenWorkspaceWorker = $false
        # The forbidden entry-point names are unique to this project. Match
        # them even when PowerShell was invoked with a relative script path;
        # requiring the absolute workspace path creates a trivial bypass.
        foreach ($marker in $ForbiddenWorkspaceProcessMarkers) {
            if ($command.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $isForbiddenWorkspaceWorker = $true
                break
            }
        }
        if ($isThisSupervisor -or $isLegacySupervisor -or $isExactRadar -or $isForbiddenWorkspaceWorker) { return $true }
    }
    return $false
}

function Get-WorkspaceRuntimeInventory {
    $supervisorCount = 0
    $radarChildCount = 0
    $forbiddenWorkerCount = 0
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $command = [string]$process.CommandLine
        if (-not $command) { continue }
        if ($command.IndexOf($RunnerPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $supervisorCount++
        }
        if ($command.IndexOf($RadarEntryPoint, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $radarChildCount++
        }
        foreach ($marker in $ForbiddenWorkspaceProcessMarkers) {
            if ($command.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $forbiddenWorkerCount++
                break
            }
        }
    }
    return [PSCustomObject]@{
        SupervisorCount = $supervisorCount
        RadarChildCount = $radarChildCount
        ForbiddenWorkerCount = $forbiddenWorkerCount
    }
}

function Write-Heartbeat {
    param(
        [string]$State,
        [int]$CycleNumber,
        [Nullable[int]]$ChildPid,
        [string]$Detail = ""
    )
    $payload = @{
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        state = $State
        supervisor_pid = $PID
        supervisor_count = 1
        child_count = if ($null -ne $ChildPid) { 1 } else { 0 }
        child_pid = $ChildPid
        cycle_number = $CycleNumber
        cadence_seconds = $CadenceSeconds
        timeout_seconds = $TimeoutSeconds
        config_path = $ResolvedConfig
        detail = $Detail
    }
    $temporary = "$HeartbeatPath.tmp.$PID"
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $HeartbeatPath -Force
}

function Stop-ExactChildTree {
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
    $targets = [System.Collections.Generic.List[int]]::new()
    for ($index = $descendants.Count - 1; $index -ge 0; $index--) {
        $targets.Add($descendants[$index])
    }
    $targets.Add($RootPid)
    foreach ($targetPid in $targets) {
        if ($null -eq (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) { continue }
        try {
            Stop-Process -Id $targetPid -Force -ErrorAction Stop
        }
        catch {
            if ($null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) { throw }
        }
    }
    foreach ($targetPid in $targets) {
        if ($null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
            try { Wait-Process -Id $targetPid -Timeout 10 -ErrorAction Stop }
            catch {
                if ($null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
                    throw "Timed-out bounded child process $targetPid could not be stopped safely."
                }
            }
        }
    }
}

$ProviderKeys = @(
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "AZURE_API_KEY",
    "COHERE_API_KEY", "CODEX_API_KEY"
)
foreach ($name in $ProviderKeys) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
$env:RADAR_REQUIRE_EXPLICIT_CONFIG = "1"
$env:RADAR_MODEL_CREDENTIAL_LOCK = "1"
$env:RADAR_MODELS_DISABLED = "1"
$env:RADAR_USE_LITELLM = "0"
$env:RADAR_PROCESS_ROLE = "bounded_paper_radar"
Remove-Item -LiteralPath "Env:RADAR_RESEARCH_MODEL_OVERRIDE" -ErrorAction SilentlyContinue
$env:RADAR_BOUNDED_SUPERVISOR_COUNT = "1"
$env:RADAR_BOUNDED_CHILD_COUNT = "1"
$env:RADAR_FORBIDDEN_WORKER_COUNT = "0"

& $PythonExe -B $PreflightEntryPoint --config $ResolvedConfig --require-process-lock
if ($LASTEXITCODE -ne 0) { throw "Bounded paper preflight failed with exit code $LASTEXITCODE" }

$IdentityHash = Get-IdentityHash
$MutexName = "Global\AgenticTradingSwarm.BoundedPaper.$IdentityHash"
$CycleMutexName = "Global\AgenticTradingSwarm.BoundedPaperCycle.$IdentityHash"
$InstanceMutex = [System.Threading.Mutex]::new($false, $MutexName)
$CycleMutex = [System.Threading.Mutex]::new($false, $CycleMutexName)
$OwnsMutex = $false
$WrotePidFile = $false
$Cycle = 0
try {
    try { $OwnsMutex = $InstanceMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $OwnsMutex = $true }
    if (-not $OwnsMutex) { throw "The exact workspace/config supervisor is already running." }
    if (Test-ExactExistingProcess) { throw "Another radar process already owns this workspace." }

    @{
        pid = $PID
        started_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        project_root = $ProjectRoot
        config_path = $ResolvedConfig
        runner_path = $RunnerPath
        mutex_name = $MutexName
    } | ConvertTo-Json | Set-Content -LiteralPath $PidPath -Encoding UTF8
    $WrotePidFile = $true

    $NextStart = [DateTimeOffset]::UtcNow
    while ($MaxCycles -le 0 -or $Cycle -lt $MaxCycles) {
        while ([DateTimeOffset]::UtcNow -lt $NextStart) {
            Write-Heartbeat -State "sleeping" -CycleNumber $Cycle -ChildPid $null
            $remaining = ($NextStart - [DateTimeOffset]::UtcNow).TotalSeconds
            Start-Sleep -Seconds ([Math]::Max(1, [Math]::Min($HeartbeatSeconds, [int][Math]::Ceiling($remaining))))
        }

        $Cycle++
        $OwnsCycleMutex = $false
        while (-not $OwnsCycleMutex) {
            try { $OwnsCycleMutex = $CycleMutex.WaitOne(0, $false) }
            catch [System.Threading.AbandonedMutexException] { $OwnsCycleMutex = $true }
            if (-not $OwnsCycleMutex) {
                Write-Heartbeat -State "waiting_for_paid_research" -CycleNumber $Cycle -ChildPid $null
                Start-Sleep -Seconds $HeartbeatSeconds
            }
        }
        try {
            $CycleStarted = [DateTimeOffset]::UtcNow
            # Cadence is measured from the actual lock-protected cycle start.
            # A paid-research delay therefore cannot compress the next interval.
            $NextStart = $CycleStarted.AddSeconds($CadenceSeconds)
            $inventory = Get-WorkspaceRuntimeInventory
            $env:RADAR_BOUNDED_SUPERVISOR_COUNT = [string]$inventory.SupervisorCount
            # The child about to be launched is included in the proof it
            # inherits; any pre-existing radar child therefore makes this >1.
            $env:RADAR_BOUNDED_CHILD_COUNT = [string]([int]$inventory.RadarChildCount + 1)
            $env:RADAR_FORBIDDEN_WORKER_COUNT = [string]$inventory.ForbiddenWorkerCount
            $childArgs = @(
                "-B", "`"$RadarEntryPoint`"", "--config", "`"$ResolvedConfig`"",
                "--iterations", "1", "--interval", "$CadenceSeconds"
            )
            $child = Start-Process -FilePath $PythonExe -ArgumentList $childArgs -WorkingDirectory $ProjectRoot -NoNewWindow -PassThru -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
            $timedOut = $false
            while (-not $child.HasExited) {
                $elapsed = ([DateTimeOffset]::UtcNow - $CycleStarted).TotalSeconds
                if ($elapsed -ge $TimeoutSeconds) {
                    $timedOut = $true
                    Stop-ExactChildTree -RootPid $child.Id
                    break
                }
                Write-Heartbeat -State "running" -CycleNumber $Cycle -ChildPid $child.Id -Detail "elapsed_seconds=$([int]$elapsed)"
                $remainingTimeoutSeconds = $TimeoutSeconds - $elapsed
                $sleepMilliseconds = [Math]::Max(
                    1,
                    [Math]::Min(
                        $HeartbeatSeconds * 1000,
                        [int][Math]::Ceiling($remainingTimeoutSeconds * 1000)
                    )
                )
                Start-Sleep -Milliseconds $sleepMilliseconds
                $child.Refresh()
            }
            $child.Refresh()
            $exitCode = if ($timedOut) { 124 } elseif ($child.HasExited) { $child.ExitCode } else { 125 }
            $runtimeSeconds = ([DateTimeOffset]::UtcNow - $CycleStarted).TotalSeconds
            if ($exitCode -ne 0) {
                $eventArgs = @(
                    "-B", $SupervisorEventEntryPoint, "--config", $ResolvedConfig,
                    "--exit-code", "$exitCode", "--runtime-seconds", "$runtimeSeconds"
                )
                if ($timedOut) { $eventArgs += "--timed-out" }
                & $PythonExe @eventArgs
                if ($LASTEXITCODE -ne 0) {
                    Write-Heartbeat -State "supervisor_failure_record_blocked" -CycleNumber $Cycle -ChildPid $null -Detail "exit_code=$exitCode recorder_exit=$LASTEXITCODE"
                    throw "Unable to persist the failed in-flight campaign cycle."
                }
            }
        }
        finally {
            if ($OwnsCycleMutex) {
                try { $CycleMutex.ReleaseMutex() } catch [System.ApplicationException] { }
                $OwnsCycleMutex = $false
            }
        }
        Write-Heartbeat -State $(if ($timedOut) { "timed_out" } elseif ($exitCode -eq 0) { "cycle_complete" } else { "cycle_failed" }) -CycleNumber $Cycle -ChildPid $null -Detail "exit_code=$exitCode"
    }
}
finally {
    if ($WrotePidFile -and (Test-Path -LiteralPath $PidPath)) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    }
    if ($OwnsMutex) {
        try { $InstanceMutex.ReleaseMutex() } catch [System.ApplicationException] { }
    }
    $InstanceMutex.Dispose()
    $CycleMutex.Dispose()
}
