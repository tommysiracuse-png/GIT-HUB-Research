param(
    [string]$Provider = "openai",
    [string]$ApiKey
)

$ErrorActionPreference = "Stop"

$keyNames = @{
    openai = "OPENAI_API_KEY"
    anthropic = "ANTHROPIC_API_KEY"
    gemini = "GEMINI_API_KEY"
    google = "GOOGLE_API_KEY"
    mistral = "MISTRAL_API_KEY"
    groq = "GROQ_API_KEY"
    openrouter = "OPENROUTER_API_KEY"
    azure = "AZURE_API_KEY"
    cohere = "COHERE_API_KEY"
}

$providerKey = $Provider.Trim().ToLowerInvariant()
$envName = $keyNames[$providerKey]
if (-not $envName) {
    $validProviders = ($keyNames.Keys | Sort-Object) -join ", "
    throw "Unsupported provider: $Provider. Valid providers: $validProviders"
}

if (-not $ApiKey) {
    $secureKey = Read-Host "Enter $envName" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $ApiKey) {
    throw "No API key was provided."
}

[Environment]::SetEnvironmentVariable($envName, $ApiKey, "User")
[Environment]::SetEnvironmentVariable($envName, $ApiKey, "Process")
[Environment]::SetEnvironmentVariable("RADAR_USE_LITELLM", "1", "User")
[Environment]::SetEnvironmentVariable("RADAR_USE_LITELLM", "1", "Process")

Write-Output "Saved $envName for this Windows user."
Write-Output "Saved RADAR_USE_LITELLM=1 for this Windows user."
Write-Output "Restart the radar supervisor for the new key to be picked up."
