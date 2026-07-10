"""Candidate canary execution helpers."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
from typing import Any


def run_radar_canary(app_root: pathlib.Path, *, timeout_seconds: int = 180, max_latency_seconds: float = 180.0) -> dict[str, Any]:
    """Run one paper radar cycle as a smoke canary in a candidate worktree."""

    started = time.monotonic()
    cmd = [sys.executable, "-B", "src/radar_loop.py", "--iterations", "1"]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(app_root),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
        latency = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "stage": "radar_canary", "reason": str(exc), "command": cmd}

    latest = app_root / "runs" / "radar_state_latest.json"
    report_fresh = latest.exists()
    live_flag_ok = True
    if latest.exists():
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
            live_flag_ok = not bool(payload.get("live_trading_allowed") or payload.get("allow_live_trading"))
        except (OSError, json.JSONDecodeError):
            live_flag_ok = False
    passed = completed.returncode == 0 and report_fresh and live_flag_ok and latency <= max_latency_seconds
    return {
        "passed": passed,
        "stage": "radar_canary",
        "command": cmd,
        "returncode": completed.returncode,
        "latency_seconds": round(latency, 3),
        "max_latency_seconds": max_latency_seconds,
        "report_fresh": report_fresh,
        "live_flag_ok": live_flag_ok,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def skip_canary(reason: str, *, stage: str = "deferred_by_policy") -> dict[str, Any]:
    return {"passed": True, "stage": stage, "reason": reason}
