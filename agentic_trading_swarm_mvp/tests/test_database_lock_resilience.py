from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cost_router
import storage


class DatabaseLockResilienceTests(unittest.TestCase):
    def test_connect_sets_busy_timeout_and_wal_for_file_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "radar.sqlite"
            with storage.connect(db) as conn:
                busy_timeout = conn.execute("pragma busy_timeout").fetchone()[0]
                journal_mode = conn.execute("pragma journal_mode").fetchone()[0]

        self.assertEqual(busy_timeout, storage.SQLITE_BUSY_TIMEOUT_MS)
        self.assertEqual(str(journal_mode).lower(), "wal")

    def test_init_db_does_not_run_noop_outcome_backfill_update(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            storage.init_db(conn)
        finally:
            conn.close()

        updates = [stmt for stmt in statements if "update paper_trade_outcomes" in stmt.lower()]
        self.assertEqual(updates, [])

    def test_spent_today_treats_database_lock_as_budget_unavailable(self) -> None:
        with mock.patch.object(cost_router, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            spent = cost_router._spent_today("build_planner")

        self.assertEqual(spent, float("inf"))

    def test_cost_log_is_deferred_when_database_is_locked(self) -> None:
        result = cost_router.ModelResult(
            text="ok",
            model_name="openai/gpt-5.4-mini",
            model_tier="fast",
            prompt_tokens=10,
            completion_tokens=5,
            estimated_cost_usd=0.01,
            status="model_call:responses",
            provider="openai",
            api="responses",
            reasoning_effort="low",
            reasoning_mode=None,
            verbosity=None,
            operation="test",
            prompt_cache_key=None,
            frontier_escalation_reason=None,
            structured_json=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            deferred = pathlib.Path(tmp) / "llm_cost_events_deferred.jsonl"
            with mock.patch.object(cost_router, "COST_LOG_DEFERRED_PATH", deferred), mock.patch.object(
                cost_router,
                "connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                cost_router._log("build_planner", result)

            rows = [json.loads(line) for line in deferred.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_name"], "build_planner")
        self.assertEqual(rows[0]["deferred_reason"], "database_locked_on_connect")


if __name__ == "__main__":
    unittest.main()
