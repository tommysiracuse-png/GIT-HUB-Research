#!/usr/bin/env python3
"""Paper-trading loop for the first inefficiency radar sensor.

This uses the OKX scanner signals and tracks simple directional outcomes.
It is a validation tool, not an execution engine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import time

from okx_perp_scanner import build_candidates
from settings import load_settings


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
DB_PATH = RUNS_DIR / "paper_trades.sqlite"


LONG_DIRECTIONS = {
    "long_perp_short_spot",
    "basis_mean_reversion_long_perp",
    "funding_capture_long_perp",
    "long_proxy",
    "long_frontier_spot",
    "long_frontier_perp",
    "buy_yes_event",
    "buy_no_event",
}
SHORT_DIRECTIONS = {
    "short_perp_long_spot",
    "basis_mean_reversion_short_perp",
    "funding_capture_short_perp",
    "short_proxy",
    "short_frontier_spot",
    "short_frontier_perp",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def direction_sign(direction: str) -> int:
    if direction in LONG_DIRECTIONS:
        return 1
    if direction in SHORT_DIRECTIONS:
        return -1
    return 0


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists paper_trades (
            id integer primary key autoincrement,
            opened_at text not null,
            closed_at text,
            venue text not null,
            inst_id text not null,
            direction text not null,
            score real not null,
            entry real not null,
            exit real,
            pnl_bps real,
            status text not null,
            thesis text not null,
            snapshot_json text not null
        )
        """
    )
    conn.execute("create index if not exists idx_paper_open on paper_trades(status, inst_id, direction)")
    conn.commit()


def has_open_trade(conn: sqlite3.Connection, inst_id: str, direction: str) -> bool:
    row = conn.execute(
        "select 1 from paper_trades where status = 'open' and inst_id = ? and direction = ? limit 1",
        (inst_id, direction),
    ).fetchone()
    return row is not None


def open_trade(conn: sqlite3.Connection, candidate: dict) -> None:
    conn.execute(
        """
        insert into paper_trades (
            opened_at, venue, inst_id, direction, score, entry, status, thesis, snapshot_json
        ) values (?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            utc_now().isoformat(),
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate["score"],
            candidate["last"],
            candidate["thesis"],
            json.dumps(candidate, sort_keys=True),
        ),
    )
    conn.commit()


def close_due_trades(conn: sqlite3.Connection, latest_by_inst: dict[str, dict], hold_minutes: int) -> list[dict]:
    now = utc_now()
    closed = []
    rows = conn.execute(
        "select id, opened_at, inst_id, direction, entry from paper_trades where status = 'open'"
    ).fetchall()
    for trade_id, opened_at, inst_id, direction, entry in rows:
        age_minutes = (now - parse_iso(opened_at)).total_seconds() / 60.0
        if age_minutes < hold_minutes:
            continue
        latest = latest_by_inst.get(inst_id)
        if not latest:
            continue
        sign = direction_sign(direction)
        if sign == 0:
            continue
        exit_px = float(latest["last"])
        pnl_bps = (exit_px / float(entry) - 1.0) * 10_000.0 * sign
        conn.execute(
            """
            update paper_trades
            set closed_at = ?, exit = ?, pnl_bps = ?, status = 'closed'
            where id = ?
            """,
            (now.isoformat(), exit_px, round(pnl_bps, 3), trade_id),
        )
        closed.append({"id": trade_id, "inst_id": inst_id, "direction": direction, "pnl_bps": round(pnl_bps, 3)})
    conn.commit()
    return closed


def performance_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("select pnl_bps from paper_trades where status = 'closed'").fetchall()
    pnls = [float(row[0]) for row in rows if row[0] is not None]
    open_count = conn.execute("select count(*) from paper_trades where status = 'open'").fetchone()[0]
    if not pnls:
        return {"closed": 0, "open": open_count, "avg_pnl_bps": None, "win_rate": None}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed": len(pnls),
        "open": open_count,
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
    }


def run_iteration(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    settings = load_settings()
    candidates = build_candidates(
        args.scan_universe,
        allow_short_spot=args.allow_short_spot,
        settings=settings,
    )
    latest_by_inst = {row["inst_id"]: row for row in candidates}
    closed = close_due_trades(conn, latest_by_inst, args.hold_minutes)
    opened = []
    for candidate in candidates:
        if len(opened) >= args.max_new:
            break
        if candidate["score"] < args.min_score:
            continue
        if candidate["direction"] == "watch_only":
            continue
        if candidate.get("paper_eligible") is False:
            continue
        if direction_sign(candidate["direction"]) == 0:
            continue
        if candidate.get("execution_feasibility", {}).get("status") == "conditional" and not args.allow_short_spot:
            continue
        if has_open_trade(conn, candidate["inst_id"], candidate["direction"]):
            continue
        open_trade(conn, candidate)
        opened.append(candidate)

    summary = performance_summary(conn)
    payload = {
        "time": utc_now().isoformat(),
        "opened": [
            {
                "inst_id": row["inst_id"],
                "direction": row["direction"],
                "score": row["score"],
                "entry": row["last"],
                "thesis": row["thesis"],
            }
            for row in opened
        ],
        "closed": closed,
        "summary": summary,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / "paper_summary_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def print_payload(payload: dict) -> None:
    print(f"\n[{payload['time']}]")
    if payload["opened"]:
        print("Opened:")
        for row in payload["opened"]:
            print(f"  {row['inst_id']:<20} {row['direction']:<31} score={row['score']:<5} entry={row['entry']}")
    else:
        print("Opened: none")
    if payload["closed"]:
        print("Closed:")
        for row in payload["closed"]:
            print(f"  #{row['id']:<4} {row['inst_id']:<20} {row['direction']:<31} pnl_bps={row['pnl_bps']}")
    else:
        print("Closed: none")
    print(f"Summary: {payload['summary']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run paper-trade validation loop.")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval", type=int, default=60, help="seconds between iterations")
    parser.add_argument("--hold-minutes", type=int, default=60)
    parser.add_argument("--min-score", type=float, default=45.0)
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--scan-universe", type=int, default=100)
    parser.add_argument(
        "--allow-short-spot",
        action="store_true",
        help="allow paper entries whose hedge requires confirmed spot borrow",
    )
    args = parser.parse_args(argv)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        for idx in range(args.iterations):
            payload = run_iteration(conn, args)
            print_payload(payload)
            if idx < args.iterations - 1:
                time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
