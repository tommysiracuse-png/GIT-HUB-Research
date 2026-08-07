"""Inspect or manually reset the bounded paper campaign state."""

from __future__ import annotations

import argparse
import json
import sqlite3

from paper_expansion_campaign import CampaignError, reset_hard_halt
from settings import SettingsError, load_settings
from storage import connect


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
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, require_explicit=True)
        campaign_id = str((settings.get("paper_expansion") or {}).get("campaign_id") or "")
        if not campaign_id:
            raise SettingsError("paper expansion campaign_id is required")
        with connect(initialize=False) as conn:
            if args.command == "status":
                report = _status(conn, campaign_id)
            else:
                report = reset_hard_halt(
                    conn,
                    settings,
                    campaign_id=campaign_id,
                    operator_reason=str(args.reason),
                    clear_stale_runtime_leases=bool(args.clear_stale_runtime_leases),
                )
    except (CampaignError, FileNotFoundError, OSError, SettingsError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
