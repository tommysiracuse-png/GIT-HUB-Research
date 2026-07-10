"""Promotion gates for autonomous code-evolution candidates."""

from __future__ import annotations

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
