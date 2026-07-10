$ErrorActionPreference = "Continue"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$RunnerPath = Join-Path $ProjectRoot "scripts\run_radar_forever.ps1"
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

foreach ($envName in $ProviderEnvNames) {
    if (-not [Environment]::GetEnvironmentVariable($envName, "Process")) {
        $userValue = [Environment]::GetEnvironmentVariable($envName, "User")
        if ($userValue) {
            [Environment]::SetEnvironmentVariable($envName, $userValue, "Process")
        }
    }
}

if (-not [Environment]::GetEnvironmentVariable("RADAR_USE_LITELLM", "Process")) {
    $userLitellm = [Environment]::GetEnvironmentVariable("RADAR_USE_LITELLM", "User")
    if ($userLitellm) {
        [Environment]::SetEnvironmentVariable("RADAR_USE_LITELLM", $userLitellm, "Process")
    }
}

Write-Output "Project root: $ProjectRoot"
Write-Output "Python:       $PythonExe"
Write-Output "LLM config:   $(Join-Path $ProjectRoot 'config\llm_config.example.yaml')"
Write-Output "Runner:       $RunnerPath"
Write-Output "Swarm latest: $(Join-Path $RunsDir 'llm_swarm_latest.json')"
Write-Output "Inbox:        $(Join-Path $RunsDir 'llm_recommendations_inbox.jsonl')"

Push-Location $ProjectRoot
try {
@'
import importlib.util
import json
import os
import pathlib
import sqlite3
from collections import Counter

root = pathlib.Path.cwd()
runs = root / "runs"
runner = root / "scripts" / "run_radar_forever.ps1"
mods = ["litellm", "langgraph", "dspy", "graphiti_core", "pydantic_ai", "openai"]
keys = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_API_KEY",
    "COHERE_API_KEY",
]
report = {
    "imports": {name: importlib.util.find_spec(name) is not None for name in mods},
    "env": {"RADAR_USE_LITELLM": os.environ.get("RADAR_USE_LITELLM"), **{key: bool(os.environ.get(key)) for key in keys}},
    "runtime": {
        "venv_python_exists": (root / ".venv" / "Scripts" / "python.exe").exists(),
        "runner_sets_litellm": runner.exists() and 'RADAR_USE_LITELLM = "1"' in runner.read_text(encoding="utf-8", errors="ignore"),
        "provider_key_ready": any(os.environ.get(key) for key in keys),
    },
}
latest = runs / "llm_swarm_latest.json"
if latest.exists():
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        statuses = Counter((rec.get("model") or {}).get("status", "unknown") for rec in data.get("recommendations", []))
        report["latest_swarm"] = {
            "generated_at": data.get("generated_at"),
            "recommendations": len(data.get("recommendations", [])),
            "model_statuses": dict(statuses),
        }
    except Exception as exc:
        report["latest_swarm"] = {"error": str(exc)}
db = runs / "radar.sqlite"
if db.exists():
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select agent_name, model_tier, model_name, status, count(*) as calls,
               round(sum(estimated_cost_usd), 6) as cost
        from llm_cost_events
        group by agent_name, model_tier, model_name, status
        order by max(id) desc
        """
    ).fetchall()
    report["cost_events"] = [dict(row) for row in rows]
print(json.dumps(report, indent=2))
'@ | & $PythonExe -B -
}
finally {
    Pop-Location
}
