$ErrorActionPreference = "Continue"

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RunsDir = Join-Path $ProjectRoot "runs"
$SrcDir = Join-Path $ProjectRoot "src"
$RunnerPath = (Resolve-Path -LiteralPath $PSCommandPath).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }
$PoolEntryPoint = [System.IO.Path]::GetFullPath((Join-Path $SrcDir "codex_worker_pool.py"))
$LogPath = Join-Path $RunsDir "codex_worker_pool_forever.log"
$PidPath = Join-Path $RunsDir "codex_worker_pool_forever.pid"
$PidMetaPath = Join-Path $RunsDir "codex_worker_pool_forever.pid.json"
$HeartbeatPath = Join-Path $RunsDir "codex_worker_pool_heartbeat.json"
$ChildStdoutPath = Join-Path $RunsDir "codex_worker_pool_child_$PID.stdout.tmp"
$ChildStderrPath = Join-Path $RunsDir "codex_worker_pool_child_$PID.stderr.tmp"
$MaxLogBytes = 10MB
$SuccessIntervalSeconds = 60
$HeartbeatIntervalSeconds = 15
$MaxIterationRuntimeMinutes = 360
$InitialBackoffSeconds = 15
$MaxBackoffSeconds = 300
$child = $null
$childStartedAt = ""

function Get-OwnershipHash {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $scope = "$([System.IO.Path]::GetFullPath($ProjectRoot).ToUpperInvariant())|$([System.IO.Path]::GetFullPath($RunnerPath).ToUpperInvariant())"
        return -join ($sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($scope)) | ForEach-Object { $_.ToString("x2") })
    }
    finally { $sha256.Dispose() }
}

function Test-ExactPathArgument {
    param([string]$CommandLine, [string]$ExpectedPath)
    if (-not $CommandLine -or -not $ExpectedPath) { return $false }
    $escaped = [Regex]::Escape([System.IO.Path]::GetFullPath($ExpectedPath))
    return [Regex]::IsMatch($CommandLine, "(?i)(?:^|\s)(?:`"$escaped`"|'$escaped'|$escaped)(?=`$|\s)")
}

function Test-ExactRunnerCommand {
    param([string]$CommandLine)
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
        return $null -ne $actual -and [Math]::Abs(($actual - $recorded).TotalSeconds) -le 2
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

function Stop-OwnedChildTree {
    param([int]$ChildProcessId, [string]$RecordedStart)
    if ($ChildProcessId -le 0 -or -not $RecordedStart) { return $true }
    $root = Get-CimInstance Win32_Process -Filter "ProcessId=$ChildProcessId" -ErrorAction SilentlyContinue
    if ($root -and ([string]$root.Name -notlike "python*.exe" -or
        [int]$root.ParentProcessId -ne $PID -or
        -not (Test-ExactPathArgument -CommandLine ([string]$root.CommandLine) -ExpectedPath $PoolEntryPoint) -or
        -not (Test-RecordedStart -Process $root -RecordedStart $RecordedStart))) { return $false }
    $tree = @(Get-TreeRows -RootPid $ChildProcessId -Snapshot @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue))
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

function Test-ExistingExactOwner {
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if ([int]$process.ProcessId -eq $PID) { continue }
        if (Test-ExactRunnerCommand -CommandLine ([string]$process.CommandLine)) { return $true }
        if ([string]$process.Name -like "python*.exe" -and
            (Test-ExactPathArgument -CommandLine ([string]$process.CommandLine) -ExpectedPath $PoolEntryPoint)) { return $true }
    }
    return $false
}

$MutexName = "Global\AgenticTradingSwarm.CodexWorkerPool.$(Get-OwnershipHash)"
$InstanceMutex = [System.Threading.Mutex]::new($false, $MutexName)
$OwnsMutex = $false
try {
    try { $OwnsMutex = $InstanceMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $OwnsMutex = $true }
    if (-not $OwnsMutex -or (Test-ExistingExactOwner)) { exit 0 }

New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

Set-Content -Path $PidPath -Value $PID -Encoding ASCII
$SupervisorStartedAt = (Get-Date).ToUniversalTime().ToString("o")
@{
    pid = $PID
    started_at_utc = $SupervisorStartedAt
    project_root = "$ProjectRoot"
    script = "run_codex_worker_pool_forever.ps1"
    script_path = $RunnerPath
    mutex_name = $MutexName
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

function Write-AtomicJson {
    param([string]$Path, $Value)
    $temporaryPath = "$Path.$PID.tmp"
    try {
        [System.IO.File]::WriteAllText($temporaryPath, ($Value | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
        if ([System.IO.File]::Exists($Path)) { [System.IO.File]::Replace($temporaryPath, $Path, $null, $true) }
        else { [System.IO.File]::Move($temporaryPath, $Path) }
    }
    finally {
        if ([System.IO.File]::Exists($temporaryPath)) { Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue }
    }
}

function Write-Heartbeat {
    param(
        [string]$Status,
        [int]$ConsecutiveFailures,
        [int]$BackoffSeconds,
        [string]$LastError = "",
        [string]$IterationStartedAtUtc = "",
        [int]$ChildProcessId = 0,
        [string]$ChildStartedAtUtc = ""
    )
    try {
        Write-AtomicJson -Path $HeartbeatPath -Value ([ordered]@{
            supervisor_pid = $PID
            status = $Status
            started_at_utc = $SupervisorStartedAt
            last_updated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            project_root = "$ProjectRoot"
            log_path = "$LogPath"
            runner_path = $RunnerPath
            entry_point = "$PoolEntryPoint"
            child_pid = if ($ChildProcessId -gt 0) { $ChildProcessId } else { $null }
            child_started_at_utc = if ($ChildStartedAtUtc) { $ChildStartedAtUtc } else { $null }
            child_entry_point = $PoolEntryPoint
            iteration_started_at_utc = if ($IterationStartedAtUtc) { $IterationStartedAtUtc } else { $null }
            max_iteration_runtime_minutes = $MaxIterationRuntimeMinutes
            consecutive_failures = $ConsecutiveFailures
            backoff_seconds = $BackoffSeconds
            last_error = $LastError
        })
    }
    catch {
        try { Write-Log "Heartbeat write failed while status=$Status child_pid=${ChildProcessId}: $($_.Exception.Message)" }
        catch {}
    }
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

    $iterationStartedAt = (Get-Date).ToUniversalTime().ToString("o")

    $exitCode = 1
    $failureMessage = ""
    $child = $null
    try {
        Write-Log "Starting one Codex worker-pool iteration."
        $child = Start-Process -FilePath $PythonExe `
            -ArgumentList @("-B", "`"$PoolEntryPoint`"", "--iterations", "1", "--interval", "$SuccessIntervalSeconds") `
            -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
            -RedirectStandardOutput $ChildStdoutPath -RedirectStandardError $ChildStderrPath -PassThru
        try { $childStartedAt = $child.StartTime.ToUniversalTime().ToString("o") }
        catch { $childStartedAt = $iterationStartedAt }
        do {
            Write-Heartbeat -Status "running_iteration" -ConsecutiveFailures $consecutiveFailures `
                -BackoffSeconds 0 -IterationStartedAtUtc $iterationStartedAt `
                -ChildProcessId $child.Id -ChildStartedAtUtc $childStartedAt
            $finished = $child.WaitForExit($HeartbeatIntervalSeconds * 1000)
        } while (-not $finished)
        $child.WaitForExit()
        $exitCode = $child.ExitCode
        foreach ($path in @($ChildStdoutPath, $ChildStderrPath)) {
            if (Test-Path -LiteralPath $path) {
                $content = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
                if ($content) { Add-Content -LiteralPath $LogPath -Value $content.TrimEnd("`r", "`n") -Encoding UTF8 }
                Clear-Content -LiteralPath $path -ErrorAction SilentlyContinue
            }
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
        if ($child) {
            # Preserve a fresh, exact child identity until the owned process exits;
            # never enter backoff and launch another paid child alongside it.
            while ($true) {
                try {
                    $child.Refresh()
                    if ($child.HasExited) { break }
                }
                catch {}
                Write-Heartbeat -Status "running_iteration" -ConsecutiveFailures $consecutiveFailures `
                    -BackoffSeconds 0 -IterationStartedAtUtc $iterationStartedAt `
                    -ChildProcessId $child.Id -ChildStartedAtUtc $childStartedAt -LastError $failureMessage
                Start-Sleep -Seconds $HeartbeatIntervalSeconds
            }
            try { $child.WaitForExit(); $exitCode = $child.ExitCode }
            catch {}
        }
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
}
finally {
    if (-not $OwnsMutex) {
        try {
            if ((Test-Path -LiteralPath $PidPath) -and [int](Get-Content -LiteralPath $PidPath -ErrorAction Stop | Select-Object -First 1) -eq $PID) {
                Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
            }
        }
        catch {}
    }
    if ($OwnsMutex) {
        try {
            $ownedChildPid = if ($child) { [int]$child.Id } else { 0 }
            Stop-OwnedChildTree -ChildProcessId $ownedChildPid -RecordedStart $childStartedAt | Out-Null
        }
        catch {}
        try {
            if ((Test-Path -LiteralPath $PidPath) -and [int](Get-Content -LiteralPath $PidPath -ErrorAction Stop | Select-Object -First 1) -eq $PID) {
                Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
            }
            $meta = if (Test-Path -LiteralPath $PidMetaPath) { Get-Content -LiteralPath $PidMetaPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
            if ($meta -and [int]$meta.pid -eq $PID) { Remove-Item -LiteralPath $PidMetaPath -Force -ErrorAction SilentlyContinue }
            $heartbeat = if (Test-Path -LiteralPath $HeartbeatPath) { Get-Content -LiteralPath $HeartbeatPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
            if ($heartbeat -and [int]$heartbeat.supervisor_pid -eq $PID) { Remove-Item -LiteralPath $HeartbeatPath -Force -ErrorAction SilentlyContinue }
            foreach ($path in @($ChildStdoutPath, $ChildStderrPath)) {
                if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
            }
        }
        catch {}
        try { $InstanceMutex.ReleaseMutex() }
        catch {}
    }
    $InstanceMutex.Dispose()
}
