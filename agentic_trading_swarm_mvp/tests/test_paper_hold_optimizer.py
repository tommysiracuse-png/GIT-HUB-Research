from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import storage
from settings import DEFAULT_SETTINGS


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def settings(**overrides: object) -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["learning"]["horizon_minutes"] = [5, 15, 60, 240]
    cfg["paper_hold_optimizer"].update(
        {
            "enabled": True,
            "candidate_horizons_minutes": [5, 15, 60, 240],
            "default_hold_minutes": 60,
            "min_samples": 3,
            "min_avg_uplift_bps": 2.0,
            "recency_weighting_enabled": True,
            "recency_half_life_days": 3.0,
            "confidence_adjustment_enabled": True,
            "confidence_target_effective_samples": 12.0,
            "confidence_floor": 0.25,
            "group_hierarchy": ["signal_key", "venue_trade_direction", "trade_direction"],
        }
    )
    cfg["paper_hold_optimizer"].update(overrides)
    return cfg


def candidate(inst_id: str = "TEST") -> dict:
    return {
        "venue": "TEST",
        "inst_id": inst_id,
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": 80.0,
        "last": 100.0,
        "thesis": "hold optimizer test",
        "execution_feasibility": {"status": "standard"},
    }


def open_trade(conn: sqlite3.Connection, inst_id: str, opened_at: str, cfg: dict | None = None) -> int:
    trade_id = storage.open_paper_trade(
        conn,
        candidate(inst_id),
        {"learned_score": 80.0, "route_status": "standard"},
        settings=cfg,
    )
    conn.execute("update paper_trades set opened_at = ? where id = ?", (opened_at, trade_id))
    conn.commit()
    return trade_id


def insert_outcome(
    conn: sqlite3.Connection,
    trade_id: int,
    horizon: int,
    pnl_bps: float,
    observed_at: dt.datetime | None = None,
) -> None:
    now = observed_at or dt.datetime.now(dt.timezone.utc)
    conn.execute(
        """
        insert into paper_trade_outcomes (
            trade_id, horizon_minutes, measured_at, price, pnl_bps, context_json,
            target_at, observed_at, delay_seconds, measurement_status, price_source
        ) values (?, ?, ?, ?, ?, '{}', ?, ?, 0, 'valid', 'unit_test')
        """,
        (
            trade_id,
            horizon,
            now.isoformat(),
            100.0 + pnl_bps / 100.0,
            pnl_bps,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()


class PaperHoldOptimizerTests(unittest.TestCase):
    def test_selects_shorter_horizon_when_signal_evidence_is_better(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        for idx in range(3):
            trade_id = open_trade(conn, f"HIST{idx}", (now - dt.timedelta(days=idx + 1)).isoformat())
            insert_outcome(conn, trade_id, 15, 25.0)
            insert_outcome(conn, trade_id, 60, -5.0)
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        conn.commit()

        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat(), settings())
        insert_outcome(conn, current_id, 15, 10.0)

        closed = storage.close_due_trades(conn, {}, 60, settings())

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["id"], current_id)
        self.assertEqual(closed[0]["hold_minutes"], 15)
        self.assertEqual(closed[0]["hold_decision"]["source"], "optimized_valid_outcomes")

    def test_falls_back_to_default_when_evidence_is_thin(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat())
        insert_outcome(conn, current_id, 15, 10.0)

        closed = storage.close_due_trades(conn, {}, 60, settings(min_samples=10))

        self.assertEqual(closed, [])
        row = conn.execute("select status from paper_trades where id = ?", (current_id,)).fetchone()
        self.assertEqual(row["status"], "open")

    def test_existing_policy_moves_one_horizon_step_at_a_time(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        cfg = settings(min_samples=3, max_horizon_steps_per_update=1, min_avg_uplift_bps=2.0, switch_uplift_bps=4.0)
        for idx in range(3):
            trade_id = open_trade(conn, f"HIST{idx}", (now - dt.timedelta(days=idx + 1)).isoformat())
            insert_outcome(conn, trade_id, 5, 45.0)
            insert_outcome(conn, trade_id, 15, 20.0)
            insert_outcome(conn, trade_id, 60, -5.0)
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        conn.commit()

        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat(), cfg)
        row = conn.execute(
            "select selected_hold_minutes, hold_decision_json from paper_trades where id = ?",
            (current_id,),
        ).fetchone()

        self.assertEqual(row["selected_hold_minutes"], 15)
        self.assertIn("optimized_gradual_step", row["hold_decision_json"])

    def test_existing_policy_sticks_without_material_uplift(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        cfg = settings(min_samples=3, switch_uplift_bps=10.0)
        conn.execute(
            """
            insert into paper_hold_policies (
                created_at, updated_at, group_name, group_value, selected_hold_minutes,
                previous_hold_minutes, source, evidence_json
            ) values (?, ?, 'signal_key', ?, 15, 60, 'unit_test', '{}')
            """,
            (
                now.isoformat(),
                now.isoformat(),
                "TEST|frontier_crypto_venue_map|long_frontier_spot|standard",
            ),
        )
        for idx in range(3):
            trade_id = open_trade(conn, f"HIST{idx}", (now - dt.timedelta(days=idx + 1)).isoformat())
            insert_outcome(conn, trade_id, 5, 18.0)
            insert_outcome(conn, trade_id, 15, 12.0)
            insert_outcome(conn, trade_id, 60, -5.0)
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        conn.commit()

        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat(), cfg)
        row = conn.execute("select selected_hold_minutes from paper_trades where id = ?", (current_id,)).fetchone()

        self.assertEqual(row["selected_hold_minutes"], 15)

    def test_recency_weighted_average_can_override_stale_average(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        cfg = settings(min_samples=6, recency_half_life_days=1.0)
        for idx in range(3):
            old_at = now - dt.timedelta(days=10 + idx)
            trade_id = open_trade(conn, f"OLD{idx}", old_at.isoformat())
            insert_outcome(conn, trade_id, 15, -50.0, old_at)
            insert_outcome(conn, trade_id, 60, 10.0, old_at)
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        for idx in range(3):
            recent_at = now - dt.timedelta(minutes=idx + 1)
            trade_id = open_trade(conn, f"RECENT{idx}", recent_at.isoformat())
            insert_outcome(conn, trade_id, 15, 60.0, recent_at)
            insert_outcome(conn, trade_id, 60, 10.0, recent_at)
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        conn.commit()

        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat(), cfg)
        row = conn.execute(
            "select selected_hold_minutes, hold_decision_json from paper_trades where id = ?",
            (current_id,),
        ).fetchone()
        decision = json.loads(row["hold_decision_json"])
        metrics = {item["horizon_minutes"]: item for item in decision["metrics"]}

        self.assertEqual(row["selected_hold_minutes"], 15)
        self.assertLess(metrics[15]["raw_avg_pnl_bps"], metrics[60]["raw_avg_pnl_bps"])
        self.assertGreater(metrics[15]["avg_pnl_bps"], metrics[60]["avg_pnl_bps"] + 2.0)

    def test_confidence_adjusted_score_prefers_better_supported_horizon(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        cfg = settings(
            min_samples=3,
            confidence_target_effective_samples=12.0,
            confidence_floor=0.25,
            min_avg_uplift_bps=1.0,
        )
        for idx in range(3):
            trade_id = open_trade(conn, f"THIN{idx}", (now - dt.timedelta(minutes=idx + 20)).isoformat())
            insert_outcome(conn, trade_id, 15, 40.0, now - dt.timedelta(minutes=idx + 20))
            insert_outcome(conn, trade_id, 60, 10.0, now - dt.timedelta(minutes=idx + 20))
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        for idx in range(12):
            trade_id = open_trade(conn, f"FULL{idx}", (now - dt.timedelta(minutes=idx + 60)).isoformat())
            insert_outcome(conn, trade_id, 60, 20.0, now - dt.timedelta(minutes=idx + 60))
            conn.execute("update paper_trades set status = 'closed' where id = ?", (trade_id,))
        conn.commit()

        current_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=61)).isoformat(), cfg)
        row = conn.execute(
            "select selected_hold_minutes, hold_decision_json from paper_trades where id = ?",
            (current_id,),
        ).fetchone()
        decision = json.loads(row["hold_decision_json"])
        metrics = {item["horizon_minutes"]: item for item in decision["metrics"]}

        self.assertEqual(row["selected_hold_minutes"], 60)
        self.assertGreater(metrics[15]["avg_pnl_bps"], metrics[60]["avg_pnl_bps"])
        self.assertLess(
            metrics[15]["confidence_adjusted_score_bps"],
            metrics[60]["confidence_adjusted_score_bps"],
        )

    def test_disabled_optimizer_uses_static_hold_minutes(self) -> None:
        conn = memory_conn()
        now = dt.datetime.now(dt.timezone.utc)
        trade_id = open_trade(conn, "CURRENT", (now - dt.timedelta(minutes=16)).isoformat())
        decision = storage.select_paper_hold_minutes(conn, conn.execute("select * from paper_trades where id = ?", (trade_id,)).fetchone(), 60, settings(enabled=False))

        self.assertEqual(decision["hold_minutes"], 60)
        self.assertEqual(decision["source"], "static_config")


if __name__ == "__main__":
    unittest.main()
