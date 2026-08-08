"""Inspect or manually reset the bounded paper campaign state."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3

from paper_expansion_campaign import (
    CampaignError,
    adopt_deferred_cost_ledger,
    reset_hard_halt,
)
from settings import SettingsError, load_settings
from storage import connect


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOUNDED_RUNTIME_MARKERS = (
    "run_bounded_paper_forever.ps1",
    "run_paid_research_supervisor.ps1",
    "run_paid_research_once.ps1",
    "radar_loop.py",
    "paid_research_once.py",
    "campaign_supervisor_event.py",
)


def _active_bounded_runtime_processes(project_root: pathlib.Path = ROOT) -> list[dict]:
    """Return live bounded workers; stale PID files do not count.

    A relative ``-File`` argument does not carry an absolute workspace token,
    so marker hits are deliberately global and conservative.  Maintenance is
    rare and must never run beside another bounded supervisor by mistake.
    """

    try:
        import psutil  # type: ignore
    except ImportError as exc:  # pragma: no cover - production dependency
        raise SettingsError("runtime process inspection is unavailable") from exc
    root_token = str(project_root.resolve()).replace("/", "\\").lower()
    matches: list[dict] = []
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        try:
            pid = int(process.info.get("pid") or 0)
            if pid == os.getpid():
                continue
            command = " ".join(str(part) for part in (process.info.get("cmdline") or []))
        except (psutil.Error, TypeError, ValueError):
            continue
        normalized = command.replace("/", "\\").lower()
        if not command:
            continue
        marker = next(
            (item for item in BOUNDED_RUNTIME_MARKERS if item.lower() in normalized),
            None,
        )
        if marker:
            try:
                process_cwd = str(process.cwd()).replace("/", "\\")
            except psutil.Error:
                process_cwd = ""
            matches.append(
                {
                    "pid": pid,
                    "name": str(process.info.get("name") or ""),
                    "marker": marker,
                    "cwd": process_cwd or None,
                    "workspace_match": bool(
                        root_token in normalized
                        or process_cwd.lower() == root_token
                    ),
                }
            )
    return sorted(matches, key=lambda item: (int(item["pid"]), str(item["marker"])))


def _status(conn: sqlite3.Connection, campaign_id: str) -> dict:
    row = conn.execute(
        "select phase,run_status,state_json from paper_expansion_campaign_state where campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if row is None:
        return {"status": "not_initialized", "campaign_id": campaign_id}
    try:
        state = json.loads(row["state_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        state = {"decode_error": True}
    return {
        "status": "ok",
        "campaign_id": campaign_id,
        "phase": row["phase"],
        "run_status": row["run_status"],
        "state": state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    reset = subparsers.add_parser("reset-hard-halt")
    reset.add_argument("--reason", required=True)
    reset.add_argument("--clear-stale-runtime-leases", action="store_true")
    adopt = subparsers.add_parser("adopt-deferred-cost-ledger")
    adopt.add_argument("--reason", required=True)
    adopt.add_argument("--expected-source-path", required=True)
    adopt.add_argument("--expected-source-sha256", required=True)
    adopt.add_argument("--expected-line-count", required=True, type=int)
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, require_explicit=True)
        campaign_id = str((settings.get("paper_expansion") or {}).get("campaign_id") or "")
        if not campaign_id:
            raise SettingsError("paper expansion campaign_id is required")
        active_processes = (
            _active_bounded_runtime_processes()
            if args.command == "adopt-deferred-cost-ledger"
            else None
        )
        with connect(initialize=False) as conn:
            if args.command == "status":
                report = _status(conn, campaign_id)
            elif args.command == "reset-hard-halt":
                report = reset_hard_halt(
                    conn,
                    settings,
                    campaign_id=campaign_id,
                    operator_reason=str(args.reason),
                    clear_stale_runtime_leases=bool(args.clear_stale_runtime_leases),
                )
            else:
                report = adopt_deferred_cost_ledger(
                    conn,
                    settings,
                    campaign_id=campaign_id,
                    operator_reason=str(args.reason),
                    expected_source_path=str(args.expected_source_path),
                    expected_source_sha256=str(args.expected_source_sha256).lower(),
                    expected_line_count=int(args.expected_line_count),
                    active_runtime_processes=active_processes,
                )
    except (CampaignError, FileNotFoundError, OSError, SettingsError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
