param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PythonExe = "python",
    [int]$CheckIntervalSeconds = 900,
    [int]$InitialDelaySeconds = 60,
    [int]$MaxChecks = 0
)

$ErrorActionPreference = "Stop"
if ($CheckIntervalSeconds -lt 60) {
    throw "CheckIntervalSeconds must be at least 60 seconds."
}
if ($InitialDelaySeconds -lt 0) {
    throw "InitialDelaySeconds must not be negative."
}

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RunnerPath = [System.IO.Path]::GetFullPath($PSCommandPath)
$OneShotRunnerPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "run_paid_research_once.ps1"))
$ResolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).Path
$RunsDir = Join-Path $ProjectRoot "runs"
$HeartbeatPath = Join-Path $RunsDir "paid_research_supervisor_heartbeat.json"
$PidPath = Join-Path $RunsDir "paid_research_supervisor.pid.json"
$LogPath = Join-Path $RunsDir "paid_research_supervisor.log"
$StdoutPath = Join-Path $RunsDir "paid_research_check.stdout.log"
$StderrPath = Join-Path $RunsDir "paid_research_check.stderr.log"
$HeartbeatSeconds = 15
$TimeoutSeconds = 300

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

function Get-IdentityHash {
    # The workspace owns one radar.sqlite; do not let config-path aliases create
    # independent supervisors or cycle mutex domains.
    $material = $ProjectRoot.ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material)))).Replace("-", "").Substring(0, 24)
    }
    finally {
        $sha.Dispose()
    }
}

function Write-Heartbeat {
    param(
        [string]$State,
        [int]$CheckNumber,
        [Nullable[int]]$ChildPid,
        [string]$Detail = ""
    )
    $payload = @{
        updated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        state = $State
        supervisor_pid = $PID
        child_pid = $ChildPid
        check_number = $CheckNumber
        check_interval_seconds = $CheckIntervalSeconds
        initial_delay_seconds = $InitialDelaySeconds
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
                    throw "Timed-out paid-research child process $targetPid could not be stopped safely."
                }
            }
        }
    }
}

$IdentityHash = Get-IdentityHash
$MutexName = "Global\AgenticTradingSwarm.PaidResearchSupervisor.$IdentityHash"
$InstanceMutex = [System.Threading.Mutex]::new($false, $MutexName)
$OwnsMutex = $false
$WrotePidFile = $false
$Check = 0
try {
    try { $OwnsMutex = $InstanceMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $OwnsMutex = $true }
    if (-not $OwnsMutex) {
        throw "The exact workspace/config paid-research supervisor is already running."
    }

    @{
        pid = $PID
        started_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        project_root = $ProjectRoot
        config_path = $ResolvedConfig
        runner_path = $RunnerPath
        mutex_name = $MutexName
        check_interval_seconds = $CheckIntervalSeconds
    } | ConvertTo-Json | Set-Content -LiteralPath $PidPath -Encoding UTF8
    $WrotePidFile = $true

    $NextCheck = [DateTimeOffset]::UtcNow.AddSeconds($InitialDelaySeconds)
    Write-Heartbeat -State "initial_delay" -CheckNumber 0 -ChildPid $null -Detail "bounded radar receives first cycle-mutex opportunity"
    while ($MaxChecks -le 0 -or $Check -lt $MaxChecks) {
        while ([DateTimeOffset]::UtcNow -lt $NextCheck) {
            $waitingState = if ($Check -eq 0) { "initial_delay" } else { "sleeping" }
            Write-Heartbeat -State $waitingState -CheckNumber $Check -ChildPid $null
            $remaining = ($NextCheck - [DateTimeOffset]::UtcNow).TotalSeconds
            Start-Sleep -Seconds ([Math]::Max(1, [Math]::Min($HeartbeatSeconds, [int][Math]::Ceiling($remaining))))
        }

        $Check++
        $CheckStarted = [DateTimeOffset]::UtcNow
        $NextCheck = $CheckStarted.AddSeconds($CheckIntervalSeconds)
        Write-Heartbeat -State "checking" -CheckNumber $Check -ChildPid $null
        $arguments = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$OneShotRunnerPath`"",
            "-ConfigPath", "`"$ResolvedConfig`"",
            "-PythonExe", "`"$PythonExe`""
        )
        $child = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
        $timedOut = $false
        while (-not $child.HasExited) {
            $elapsed = ([DateTimeOffset]::UtcNow - $CheckStarted).TotalSeconds
            if ($elapsed -ge $TimeoutSeconds) {
                $timedOut = $true
                Stop-ExactChildTree -RootPid $child.Id
                break
            }
            Write-Heartbeat -State "checking" -CheckNumber $Check -ChildPid $child.Id -Detail "elapsed_seconds=$([int]$elapsed)"
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
        if ($exitCode -eq 75) {
            $NextCheck = [DateTimeOffset]::UtcNow.AddSeconds(60)
            Write-Heartbeat -State "waiting_for_bounded_cycle" -CheckNumber $Check -ChildPid $null -Detail "retry_seconds=60"
        }
        else {
            Write-Heartbeat -State $(if ($timedOut) { "check_timed_out" } elseif ($exitCode -eq 0) { "check_complete" } else { "check_blocked" }) -CheckNumber $Check -ChildPid $null -Detail "exit_code=$exitCode"
        }
        $completedAt = [DateTimeOffset]::UtcNow.ToString("o")
        "${completedAt}`tcheck=$Check`texit_code=$exitCode" | Out-File -LiteralPath $LogPath -Encoding UTF8 -Append
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
}
