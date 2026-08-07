param(
    [int]$RadarStaleMinutes = 30,
    [int]$EvolutionStaleMinutes = 45,
    [int]$CodexWorkerPoolStaleMinutes = 15,
    [int]$RadarMaxIterationMinutes = 240,
    [int]$EvolutionMaxIterationMinutes = 240,
    [int]$CodexWorkerPoolMaxIterationMinutes = 360
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunsDir = Join-Path $ProjectRoot "runs"
$RadarEntryPoint = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "src\radar_loop.py")).Path
$EvolutionEntryPoint = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "src\evolution_worker.py")).Path
$CodexPoolEntryPoint = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "src\codex_worker_pool.py")).Path
$ProjectPrefix = $ProjectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
foreach ($entryPoint in @($RadarEntryPoint, $EvolutionEntryPoint, $CodexPoolEntryPoint)) {
    if (-not $entryPoint.StartsWith($ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Supervisor entry point is outside the exact project workspace: $entryPoint"
    }
}
New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
}

function Get-PidValue {
    param([string]$Path)
    try { return [int](Get-Content -LiteralPath $Path -ErrorAction Stop | Select-Object -First 1) }
    catch { return 0 }
}

function Test-PathEqual {
    param([string]$Left, [string]$Right)
    if (-not $Left -or -not $Right) { return $false }
    try {
        return [string]::Equals([System.IO.Path]::GetFullPath($Left), [System.IO.Path]::GetFullPath($Right), [System.StringComparison]::OrdinalIgnoreCase)
    }
    catch { return $false }
}

function Test-ExactPathArgument {
    param([string]$CommandLine, [string]$ExpectedPath)
    if (-not $CommandLine -or -not $ExpectedPath) { return $false }
    $escaped = [Regex]::Escape([System.IO.Path]::GetFullPath($ExpectedPath))
    return [Regex]::IsMatch($CommandLine, "(?i)(?:^|\s)(?:`"$escaped`"|'$escaped'|$escaped)(?=`$|\s)")
}

function Test-ExactRunnerCommand {
    param([string]$CommandLine, [string]$RunnerPath)
    if (-not $CommandLine) { return $false }
    $escaped = [Regex]::Escape([System.IO.Path]::GetFullPath($RunnerPath))
    return [Regex]::IsMatch($CommandLine, "(?i)(?:^|\s)-File\s+(?:`"$escaped`"|'$escaped')(?=`$|\s)")
}

function Get-ProcessStartUtc {
    param($Process)
    try {
        if ($Process.CreationDate -is [DateTime]) { return $Process.CreationDate.ToUniversalTime() }
        return [Management.ManagementDateTimeConverter]::ToDateTime([string]$Process.CreationDate).ToUniversalTime()
    }
    catch { return $null }
}

function Test-RecordedStart {
    param($Process, [string]$RecordedStart)
    try {
        $actual = Get-ProcessStartUtc $Process
        $recorded = [DateTimeOffset]::Parse($RecordedStart).UtcDateTime
        return $null -ne $actual -and [Math]::Abs(($actual - $recorded).TotalMinutes) -le 2
    }
    catch { return $false }
}

function Test-SameProcess {
    param($Expected, $Actual)
    if (-not $Expected -or -not $Actual -or [int]$Expected.ProcessId -ne [int]$Actual.ProcessId) { return $false }
    $left = Get-ProcessStartUtc $Expected
    $right = Get-ProcessStartUtc $Actual
    return $null -ne $left -and $null -ne $right -and [Math]::Abs(($left - $right).TotalSeconds) -le 2
}

function Get-ExactSupervisors {
    param([string]$RunnerPath)
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        Test-ExactRunnerCommand -CommandLine ([string]$_.CommandLine) -RunnerPath $RunnerPath
    })
}

function Get-ExactEntryPointProcesses {
    param([string]$EntryPoint)
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        [string]$_.Name -like "python*.exe" -and
        (Test-ExactPathArgument -CommandLine ([string]$_.CommandLine) -ExpectedPath $EntryPoint)
    })
}

function Test-WorkspacePythonChild {
    param($Process, [string]$EntryPoint, [int]$ParentPid = 0, [string]$RecordedStart = "")
    if (-not $Process -or [string]$Process.Name -notlike "python*.exe") { return $false }
    if (-not (Test-ExactPathArgument -CommandLine ([string]$Process.CommandLine) -ExpectedPath $EntryPoint)) { return $false }
    if ($ParentPid -gt 0 -and [int]$Process.ParentProcessId -ne $ParentPid) { return $false }
    return -not $RecordedStart -or (Test-RecordedStart -Process $Process -RecordedStart $RecordedStart)
}

function Test-RadarChild {
    param($Process, [string]$EntryPoint, [int]$ParentPid = 0, [string]$RecordedStart = "")
    return Test-WorkspacePythonChild -Process $Process -EntryPoint $EntryPoint -ParentPid $ParentPid -RecordedStart $RecordedStart
}

function Test-Supervisor {
    param($Definition, [string]$RunnerPath)
    $ownerPid = Get-PidValue $Definition.Pid
    if ($ownerPid -le 0) { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
    if (-not $process -or -not (Test-ExactRunnerCommand -CommandLine ([string]$process.CommandLine) -RunnerPath $RunnerPath)) { return $false }
    $owners = @(Get-ExactSupervisors $RunnerPath)
    if ($owners.Count -ne 1 -or [int]$owners[0].ProcessId -ne $ownerPid) { return $false }

    $meta = Read-JsonFile $Definition.Meta
    $heartbeat = Read-JsonFile $Definition.Heartbeat
    if (-not $meta -or [int]$meta.pid -ne $ownerPid -or -not (Test-PathEqual ([string]$meta.project_root) $ProjectRoot) -or
        -not (Test-RecordedStart -Process $process -RecordedStart ([string]$meta.started_at_utc)) -or
        -not $heartbeat -or [int]$heartbeat.supervisor_pid -ne $ownerPid -or -not (Test-PathEqual ([string]$heartbeat.project_root) $ProjectRoot)) { return $false }

    $stamp = if ($heartbeat.last_updated_at_utc) { $heartbeat.last_updated_at_utc } elseif ($heartbeat.last_iteration_finished_at_utc) { $heartbeat.last_iteration_finished_at_utc } else { $heartbeat.started_at_utc }
    try {
        if (((Get-Date).ToUniversalTime() - [DateTimeOffset]::Parse([string]$stamp).UtcDateTime).TotalMinutes -gt $Definition.Stale) { return $false }
    }
    catch { return $false }

    if (-not (Test-PathEqual ([string]$meta.script_path) $RunnerPath) -or
        -not (Test-PathEqual ([string]$heartbeat.runner_path) $RunnerPath)) { return $false }
    $status = [string]$heartbeat.status
    if (@($Definition.AllowedStatuses) -notcontains $status) { return $false }
    $entryPointProcesses = @(Get-ExactEntryPointProcesses -EntryPoint ([string]$Definition.EntryPoint))
    $isActive = @($Definition.ActiveStatuses) -contains $status
    if ($isActive) {
        try {
            $iterationStarted = [DateTimeOffset]::Parse([string]$heartbeat.iteration_started_at_utc).UtcDateTime
            $iterationAgeMinutes = ((Get-Date).ToUniversalTime() - $iterationStarted).TotalMinutes
            if ($iterationAgeMinutes -lt 0 -or $Definition.MaxIteration -le 0 -or
                $iterationAgeMinutes -gt $Definition.MaxIteration) { return $false }
        }
        catch { return $false }
        $child = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$heartbeat.child_pid)" -ErrorAction SilentlyContinue
        if (-not (Test-PathEqual ([string]$heartbeat.child_entry_point) ([string]$Definition.EntryPoint)) -or
            -not (Test-WorkspacePythonChild -Process $child -EntryPoint ([string]$Definition.EntryPoint) -ParentPid $ownerPid -RecordedStart ([string]$heartbeat.child_started_at_utc))) { return $false }
        if ($entryPointProcesses.Count -ne 1 -or
            [int]$entryPointProcesses[0].ProcessId -ne [int]$heartbeat.child_pid) { return $false }
    }
    elseif ($entryPointProcesses.Count -ne 0) {
        # Sleeping/backoff/starting supervisors are unhealthy if a legacy or
        # orphaned exact-workspace iteration is still alive.
        return $false
    }
    return $true
}

function Get-TreeRows {
    param([int]$RootPid, [object[]]$Snapshot)
    $rows = @()
    $queue = @([pscustomobject]@{ Pid = $RootPid; Depth = 0 })
    $seen = @{}
    while ($queue.Count -gt 0) {
        $next = @()
        foreach ($node in $queue) {
            if ($seen.ContainsKey([int]$node.Pid)) { continue }
            $seen[[int]$node.Pid] = $true
            $process = @($Snapshot | Where-Object { [int]$_.ProcessId -eq [int]$node.Pid }) | Select-Object -First 1
            if ($process) { $rows += [pscustomobject]@{ Process = $process; Depth = [int]$node.Depth } }
            $next += @($Snapshot | Where-Object { [int]$_.ParentProcessId -eq [int]$node.Pid } | ForEach-Object {
                [pscustomobject]@{ Pid = [int]$_.ProcessId; Depth = [int]$node.Depth + 1 }
            })
        }
        $queue = $next
    }
    return @($rows)
}

function Stop-VerifiedRadarTree {
    param($ExpectedRoot)
    $root = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$ExpectedRoot.ProcessId)" -ErrorAction SilentlyContinue
    if ($root -and (-not (Test-SameProcess $ExpectedRoot $root) -or
        -not (Test-RadarChild -Process $root -EntryPoint $RadarEntryPoint))) { return $false }
    $tree = @(Get-TreeRows -RootPid ([int]$ExpectedRoot.ProcessId) -Snapshot @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue))
    foreach ($row in @($tree | Sort-Object Depth -Descending)) {
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$row.Process.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $row.Process $actual)) { Stop-Process -Id ([int]$actual.ProcessId) -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 250
    foreach ($row in $tree) {
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$row.Process.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $row.Process $actual)) { return $false }
    }
    return $true
}

function Stop-RadarWorkspace {
    param([string]$RunnerPath, [string]$HeartbeatPath)
    $supervisors = @(Get-ExactSupervisors $RunnerPath)
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $roots = @{}
    foreach ($supervisor in $supervisors) {
        foreach ($candidate in @($snapshot | Where-Object { [int]$_.ParentProcessId -eq [int]$supervisor.ProcessId })) {
            if (Test-RadarChild -Process $candidate -EntryPoint $RadarEntryPoint -ParentPid ([int]$supervisor.ProcessId)) {
                $roots[[int]$candidate.ProcessId] = $candidate
            }
            else { return $false }
        }
    }
    $heartbeat = Read-JsonFile $HeartbeatPath
    if ($heartbeat -and (Test-PathEqual ([string]$heartbeat.project_root) $ProjectRoot) -and
        (Test-PathEqual ([string]$heartbeat.runner_path) $RunnerPath) -and
        (Test-PathEqual ([string]$heartbeat.child_entry_point) $RadarEntryPoint) -and [int]$heartbeat.child_pid -gt 0) {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$heartbeat.child_pid)" -ErrorAction SilentlyContinue
        if (Test-RadarChild -Process $candidate -EntryPoint $RadarEntryPoint -ParentPid ([int]$heartbeat.supervisor_pid) -RecordedStart ([string]$heartbeat.child_started_at_utc)) { $roots[[int]$candidate.ProcessId] = $candidate }
    }
    $workspaceChildren = @($snapshot | Where-Object {
        [string]$_.Name -like "python*.exe" -and
        (Test-ExactPathArgument -CommandLine ([string]$_.CommandLine) -ExpectedPath $RadarEntryPoint)
    })
    foreach ($candidate in $workspaceChildren) {
        if (-not $roots.ContainsKey([int]$candidate.ProcessId)) { return $false }
    }
    foreach ($root in $roots.Values) {
        if (-not (Stop-VerifiedRadarTree $root)) { return $false }
    }
    foreach ($supervisor in $supervisors) {
        $currentChildren = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            [int]$_.ParentProcessId -eq [int]$supervisor.ProcessId
        })
        if ($currentChildren.Count -gt 0) { return $false }
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$supervisor.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $supervisor $actual) -and
            (Test-ExactRunnerCommand ([string]$actual.CommandLine) $RunnerPath)) {
            Stop-Process -Id ([int]$actual.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 250
    if (@(Get-ExactSupervisors $RunnerPath).Count -gt 0) { return $false }
    $remaining = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        [string]$_.Name -like "python*.exe" -and (Test-ExactPathArgument ([string]$_.CommandLine) $RadarEntryPoint)
    })
    return $remaining.Count -eq 0
}

function Stop-VerifiedWorkerTree {
    param($ExpectedRoot, [string]$EntryPoint)
    $root = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$ExpectedRoot.ProcessId)" -ErrorAction SilentlyContinue
    if ($root) {
        if (-not (Test-SameProcess $ExpectedRoot $root) -or
            -not (Test-WorkspacePythonChild -Process $root -EntryPoint $EntryPoint)) { return $false }
    }

    # Even if the verified Python root exited between snapshots, Windows retains
    # its PID as ParentProcessId on surviving descendants. Walk that lineage so a
    # just-orphaned paid child is still included in the exact tree cleanup.
    $tree = @(Get-TreeRows -RootPid ([int]$ExpectedRoot.ProcessId) -Snapshot @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue))
    foreach ($row in @($tree | Sort-Object Depth -Descending)) {
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$row.Process.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $row.Process $actual)) {
            Stop-Process -Id ([int]$actual.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 250
    foreach ($row in $tree) {
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$row.Process.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $row.Process $actual)) { return $false }
    }
    return $true
}

function Stop-WorkerWorkspace {
    param($Definition, [string]$RunnerPath)
    $entryPoint = [string]$Definition.EntryPoint
    $supervisors = @(Get-ExactSupervisors $RunnerPath)
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $roots = @{}

    foreach ($supervisor in $supervisors) {
        foreach ($candidate in @($snapshot | Where-Object { [int]$_.ParentProcessId -eq [int]$supervisor.ProcessId })) {
            if (Test-WorkspacePythonChild -Process $candidate -EntryPoint $entryPoint -ParentPid ([int]$supervisor.ProcessId)) {
                $roots[[int]$candidate.ProcessId] = $candidate
            }
            else {
                # Never kill a supervisor while it owns an unverified child; that
                # could orphan a paid process whose command does not match this workspace.
                return $false
            }
        }
    }

    $heartbeat = Read-JsonFile $Definition.Heartbeat
    if ($heartbeat -and (Test-PathEqual ([string]$heartbeat.project_root) $ProjectRoot) -and
        (Test-PathEqual ([string]$heartbeat.runner_path) $RunnerPath) -and
        (Test-PathEqual ([string]$heartbeat.child_entry_point) $entryPoint) -and [int]$heartbeat.child_pid -gt 0) {
        $candidate = @($snapshot | Where-Object { [int]$_.ProcessId -eq [int]$heartbeat.child_pid }) | Select-Object -First 1
        if (Test-WorkspacePythonChild -Process $candidate -EntryPoint $entryPoint `
            -ParentPid ([int]$heartbeat.supervisor_pid) -RecordedStart ([string]$heartbeat.child_started_at_utc)) {
            $roots[[int]$candidate.ProcessId] = $candidate
        }
    }

    $workspaceChildren = @($snapshot | Where-Object {
        [string]$_.Name -like "python*.exe" -and
        (Test-ExactPathArgument -CommandLine ([string]$_.CommandLine) -ExpectedPath $entryPoint)
    })
    foreach ($candidate in $workspaceChildren) {
        if (-not $roots.ContainsKey([int]$candidate.ProcessId)) {
            # Exact entry point alone is not enough: without a verified parent or
            # fresh heartbeat identity, PID reuse and an unrelated launch are ambiguous.
            return $false
        }
    }

    foreach ($root in $roots.Values) {
        if (-not (Stop-VerifiedWorkerTree -ExpectedRoot $root -EntryPoint $entryPoint)) { return $false }
    }
    foreach ($supervisor in $supervisors) {
        $currentChildren = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            [int]$_.ParentProcessId -eq [int]$supervisor.ProcessId
        })
        if ($currentChildren.Count -gt 0) {
            # The supervisor changed state after the verified snapshot. Leave it
            # untouched and let the next watchdog pass acquire a fresh identity.
            return $false
        }
        $actual = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$supervisor.ProcessId)" -ErrorAction SilentlyContinue
        if ($actual -and (Test-SameProcess $supervisor $actual) -and
            (Test-ExactRunnerCommand -CommandLine ([string]$actual.CommandLine) -RunnerPath $RunnerPath)) {
            Stop-Process -Id ([int]$actual.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 250
    if (@(Get-ExactSupervisors $RunnerPath).Count -gt 0) { return $false }
    $remaining = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        [string]$_.Name -like "python*.exe" -and
        (Test-ExactPathArgument -CommandLine ([string]$_.CommandLine) -ExpectedPath $entryPoint)
    })
    return $remaining.Count -eq 0
}

function Start-Supervisor {
    param($Definition, [string]$RunnerPath)
    $process = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$RunnerPath`"") `
        -WindowStyle Hidden -PassThru
    if ($Definition.Name -ne "radar") { Set-Content -LiteralPath $Definition.Pid -Value $process.Id -Encoding ASCII }
    Write-Output "Started $($Definition.Name) supervisor with PID $($process.Id)"
}

function Get-RootHash {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $normalized = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar).ToUpperInvariant()
        return -join ($sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalized)) | ForEach-Object { $_.ToString("x2") })
    }
    finally { $sha256.Dispose() }
}

$definitions = @(
    @{ Name = "radar"; Script = "run_radar_forever.ps1"; EntryPoint = $RadarEntryPoint; Pid = Join-Path $RunsDir "radar_forever.pid"; Meta = Join-Path $RunsDir "radar_forever.pid.json"; Heartbeat = Join-Path $RunsDir "radar_heartbeat.json"; Stale = $RadarStaleMinutes; MaxIteration = $RadarMaxIterationMinutes; ActiveStatuses = @("running_iteration"); AllowedStatuses = @("starting", "running_iteration", "sleeping") },
    @{ Name = "evolution"; Script = "run_evolution_worker_forever.ps1"; EntryPoint = $EvolutionEntryPoint; Pid = Join-Path $RunsDir "evolution_worker_forever.pid"; Meta = Join-Path $RunsDir "evolution_worker_forever.pid.json"; Heartbeat = Join-Path $RunsDir "evolution_worker_heartbeat.json"; Stale = $EvolutionStaleMinutes; MaxIteration = $EvolutionMaxIterationMinutes; ActiveStatuses = @("running_iteration"); AllowedStatuses = @("running_iteration", "sleeping") },
    @{ Name = "codex_worker_pool"; Script = "run_codex_worker_pool_forever.ps1"; EntryPoint = $CodexPoolEntryPoint; Pid = Join-Path $RunsDir "codex_worker_pool_forever.pid"; Meta = Join-Path $RunsDir "codex_worker_pool_forever.pid.json"; Heartbeat = Join-Path $RunsDir "codex_worker_pool_heartbeat.json"; Stale = $CodexWorkerPoolStaleMinutes; MaxIteration = $CodexWorkerPoolMaxIterationMinutes; ActiveStatuses = @("running_iteration", "running_pool_iteration"); AllowedStatuses = @("waiting_for_entry_point", "running_iteration", "running_pool_iteration", "sleeping", "backing_off") }
)

$starterMutex = [System.Threading.Mutex]::new($false, "Global\AgenticTradingSwarm.SystemStarter.$(Get-RootHash)")
$ownsStarterMutex = $false
try {
    try { $ownsStarterMutex = $starterMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $ownsStarterMutex = $true }
    if (-not $ownsStarterMutex) { Write-Output "Another exact-workspace system start check is already in progress."; exit 0 }

    foreach ($definition in $definitions) {
        $runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot $definition.Script)).Path
        if (Test-Supervisor -Definition $definition -RunnerPath $runner) {
            Write-Output "$($definition.Name) supervisor is healthy."
            continue
        }

        if ($definition.Name -eq "radar") {
            if (-not (Stop-RadarWorkspace -RunnerPath $runner -HeartbeatPath $definition.Heartbeat)) {
                Write-Output "Radar restart skipped: an exact-workspace radar process could not be verified for safe termination."
                continue
            }
        }
        else {
            if (-not (Stop-WorkerWorkspace -Definition $definition -RunnerPath $runner)) {
                Write-Output "$($definition.Name) restart skipped: an in-flight exact-workspace child tree could not be verified for safe termination."
                continue
            }
        }
        Start-Supervisor -Definition $definition -RunnerPath $runner
    }
}
finally {
    if ($ownsStarterMutex) {
        try { $starterMutex.ReleaseMutex() }
        catch {}
    }
    $starterMutex.Dispose()
}
