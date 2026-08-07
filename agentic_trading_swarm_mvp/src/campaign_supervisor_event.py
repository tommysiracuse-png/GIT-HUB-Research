"""Record a radar child failure against the durable in-flight campaign cycle."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

from paper_expansion_campaign import CampaignError, record_inflight_failure
from settings import SettingsError, load_settings
from storage import connect


def _env_count(name: str) -> int | None:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--runtime-seconds", required=True, type=float)
    parser.add_argument("--timed-out", action="store_true")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, require_explicit=True)
        with connect() as conn:
            report = record_inflight_failure(
                conn,
                settings,
                metrics={
                    "cycle_success": False,
                    "exit_code": args.exit_code,
                    "runtime_seconds": max(0.0, args.runtime_seconds),
                    "timed_out": bool(args.timed_out),
                    "supervisor_count": _env_count("RADAR_BOUNDED_SUPERVISOR_COUNT"),
                    "child_count": _env_count("RADAR_BOUNDED_CHILD_COUNT"),
                    "forbidden_worker_count": _env_count(
                        "RADAR_FORBIDDEN_WORKER_COUNT"
                    ),
                    "supervisor_recorded_failure": True,
                },
            )
    except (CampaignError, FileNotFoundError, OSError, SettingsError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
