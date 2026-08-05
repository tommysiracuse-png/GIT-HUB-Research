"""Persistent round-robin ownership for the single Codex writer."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any


LANES = ("strategy", "adapter", "activation", "general")
SCHEDULER_KEY = "codex_writer_round_robin_v1"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _state(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "select * from evolution_owner_scheduler where scheduler_key = ?",
        (SCHEDULER_KEY,),
    ).fetchone()
    if row:
        item = dict(row)
        try:
            item["history"] = json.loads(item.pop("history_json") or "[]")
        except json.JSONDecodeError:
            item["history"] = []
        return item
    now = _utc_now()
    conn.execute(
        "insert into evolution_owner_scheduler(scheduler_key,next_lane,last_lane,turn_number,updated_at,history_json) values (?, 'strategy', null, 0, ?, '[]')",
        (SCHEDULER_KEY, now),
    )
    conn.commit()
    return {
        "scheduler_key": SCHEDULER_KEY,
        "next_lane": "strategy",
        "last_lane": None,
        "turn_number": 0,
        "updated_at": now,
        "history": [],
    }


def lane_order(conn: sqlite3.Connection) -> tuple[list[str], dict]:
    state = _state(conn)
    start = LANES.index(state["next_lane"]) if state.get("next_lane") in LANES else 0
    return [LANES[(start + offset) % len(LANES)] for offset in range(len(LANES))], state


def record_turn(
    conn: sqlite3.Connection,
    lane: str,
    *,
    cycle_id: str,
    status: str,
    consumed_writer: bool,
) -> dict:
    state = _state(conn)
    history = list(state.get("history") or [])
    entry = {
        "at": _utc_now(),
        "cycle_id": cycle_id,
        "lane": lane,
        "status": status,
        "consumed_writer": bool(consumed_writer),
    }
    history.append(entry)
    if consumed_writer:
        next_lane = LANES[(LANES.index(lane) + 1) % len(LANES)]
        turn_number = int(state.get("turn_number") or 0) + 1
        last_lane = lane
    else:
        next_lane = str(state.get("next_lane") or "strategy")
        turn_number = int(state.get("turn_number") or 0)
        last_lane = state.get("last_lane")
    conn.execute(
        """
        update evolution_owner_scheduler
        set next_lane=?, last_lane=?, turn_number=?, updated_at=?, history_json=?
        where scheduler_key=?
        """,
        (next_lane, last_lane, turn_number, _utc_now(), json.dumps(history[-100:], sort_keys=True), SCHEDULER_KEY),
    )
    conn.commit()
    return {**entry, "next_lane": next_lane, "turn_number": turn_number, "history": history[-20:]}


def scheduler_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    state = _state(conn)
    counts = {lane: 0 for lane in LANES}
    for item in state.get("history") or []:
        if item.get("consumed_writer") and item.get("lane") in counts:
            counts[item["lane"]] += 1
    return {
        "next_lane": state.get("next_lane"),
        "last_lane": state.get("last_lane"),
        "turn_number": int(state.get("turn_number") or 0),
        "turns_by_lane": counts,
        "recent_turns": (state.get("history") or [])[-20:],
    }
