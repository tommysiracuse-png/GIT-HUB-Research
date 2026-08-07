param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$EntryPoint = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "src\paid_research_once.py"))
$ResolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).Path

# Every supported config in this workspace writes to the same radar.sqlite.
# Config-path aliases must therefore share the exact same cycle mutex.
$material = $ProjectRoot.ToLowerInvariant()
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $IdentityHash = ([System.BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($material)))).Replace("-", "").Substring(0, 24)
}
finally {
    $sha.Dispose()
}
$CycleMutexName = "Global\AgenticTradingSwarm.BoundedPaperCycle.$IdentityHash"
$CycleMutex = [System.Threading.Mutex]::new($false, $CycleMutexName)
$OwnsCycleMutex = $false
$ResearchExitCode = 1

# This override exists only in this short-lived process.  The continuous radar
# supervisor never sets it and continues to strip all provider credentials.
Remove-Item -LiteralPath "Env:RADAR_MODEL_CREDENTIAL_LOCK" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "Env:RADAR_MODELS_DISABLED" -ErrorAction SilentlyContinue
$env:RADAR_PROCESS_ROLE = "research_one_shot"
$env:RADAR_RESEARCH_MODEL_OVERRIDE = "1"
$env:RADAR_USE_LITELLM = "1"
$env:RADAR_REQUIRE_EXPLICIT_CONFIG = "1"

try {
    try { $OwnsCycleMutex = $CycleMutex.WaitOne(0, $false) }
    catch [System.Threading.AbandonedMutexException] { $OwnsCycleMutex = $true }
    if (-not $OwnsCycleMutex) {
        Write-Output '{"status":"blocked","reason":"bounded_radar_cycle_active"}'
        $ResearchExitCode = 75
    }
    else {
        & $PythonExe -B $EntryPoint --config $ResolvedConfig
        $ResearchExitCode = $LASTEXITCODE
    }
}
finally {
    if ($OwnsCycleMutex) {
        try { $CycleMutex.ReleaseMutex() } catch [System.ApplicationException] { }
    }
    $CycleMutex.Dispose()
}
exit $ResearchExitCode
