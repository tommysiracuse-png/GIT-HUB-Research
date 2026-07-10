"""Promotion gates for autonomous code-evolution candidates."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any


def classify_sandbox_failure(sandbox: dict[str, Any]) -> str:
    stage = sandbox.get("stage")
    if stage in {"patch_check", "patch_apply"}:
        return "discarded_patch_apply_failure"
    if stage == "tests":
        commands = sandbox.get("commands") or []
        text = "\n".join(str(cmd.get("stderr_tail") or "") for cmd in commands if isinstance(cmd, dict))
        if "ModuleNotFoundError: No module named" in text or "NO TESTS RAN" in text:
            return "discarded_invalid_test_command"
        return "discarded_test_failure"
    if stage == "dependency_install":
        return "dependency_install_failed"
    return "implementation_failed"


def evaluate_candidate(
    *,
    sandbox: dict[str, Any],
    canary: dict[str, Any],
    category: str,
    changed_files: list[str],
) -> dict[str, Any]:
    if not sandbox.get("passed"):
        return {"passed": False, "status": classify_sandbox_failure(sandbox), "reason": "sandbox_failed"}
    if not canary.get("passed"):
        return {"passed": False, "status": "archived_failed", "reason": "canary_failed"}
    if category in {"public_data_adapter", "scanner_expansion", "quality_scoring"} and not changed_files:
        return {"passed": False, "status": "archived_failed", "reason": "no_capability_change"}
    if canary.get("stage") == "deferred_by_policy":
        return {"passed": True, "status": "promoted", "reason": "candidate passed sandbox gates; canary deferred by policy"}
    return {"passed": True, "status": "promoted", "reason": "candidate passed sandbox and canary gates"}


def benchmark_builder_change(summary: dict[str, Any]) -> dict[str, Any]:
    before = float(summary.get("before_solve_rate") or 0.0)
    after = float(summary.get("after_solve_rate") or 0.0)
    return {
        "passed": after >= before,
        "before_solve_rate": before,
        "after_solve_rate": after,
        "uplift": round(after - before, 4),
    }


def run_builder_failure_benchmark(app_root: pathlib.Path, *, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a small in-process benchmark for recurring code-evolution failures."""

    script = r'''
import json
import pathlib
import sys

root = pathlib.Path.cwd()
sys.path.insert(0, str(root / "src"))
import code_evolution
from evolution.evaluator import classify_sandbox_failure

settings = {"allow_live_trading": False, "code_evolution": {"run_full_regression": False}}
checks = []

payload = {
    "action": "propose_code_change",
    "priority": 90,
    "change_category": "public_data_adapter",
    "expected_files": ["src/radar/frontier_crypto_venues.py", "tests/test_frontier_crypto_venues.py"],
    "tests_to_run": ["pytest tests/test_frontier_crypto_venues.py"],
    "proposed_change": "Improve frontier parser wiring.",
}
preflight = code_evolution.preflight_proposal(payload, settings, root=root)
checks.append(("invalid_path_repair", "src/frontier_crypto_adapter.py" in preflight.get("target_files", [])))
checks.append(("invalid_test_repair", not preflight.get("test_issues")))
checks.append(("malformed_diff_detection", not code_evolution.changed_files_from_diff("not a diff")))
checks.append(("patch_apply_taxonomy", classify_sandbox_failure({"stage": "patch_check"}) == "discarded_patch_apply_failure"))
no_op = code_evolution.validate_and_scan(payload, "", settings, preflight=preflight)
checks.append(("no_op_detection", not no_op.get("allowed") and "no_changed_files" in no_op.get("reasons", [])))

passed = sum(1 for _name, ok in checks if ok)
print(json.dumps({
    "passed": passed == len(checks),
    "passed_count": passed,
    "total_count": len(checks),
    "solve_rate": passed / len(checks),
    "checks": [{"name": name, "passed": bool(ok)} for name, ok in checks],
}, sort_keys=True))
'''
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(app_root),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "reason": str(exc), "solve_rate": 0.0}
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {"passed": False, "reason": "invalid_benchmark_output", "solve_rate": 0.0}
    payload["returncode"] = completed.returncode
    payload["stdout_tail"] = completed.stdout[-2000:]
    payload["stderr_tail"] = completed.stderr[-2000:]
    if completed.returncode != 0:
        payload["passed"] = False
        payload.setdefault("reason", "benchmark_process_failed")
    return payload
