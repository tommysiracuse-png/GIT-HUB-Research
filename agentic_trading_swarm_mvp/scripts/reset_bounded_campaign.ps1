param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$Reason,
    [switch]$ClearStaleRuntimeLeases,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$EntryPoint = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "src\bounded_campaign_control.py"))
$ResolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).Path
$arguments = @(
    "-B", $EntryPoint, "--config", $ResolvedConfig, "reset-hard-halt", "--reason", $Reason
)
if ($ClearStaleRuntimeLeases) {
    $arguments += "--clear-stale-runtime-leases"
}
& $PythonExe @arguments
exit $LASTEXITCODE
