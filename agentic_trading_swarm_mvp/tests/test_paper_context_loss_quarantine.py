from __future__ import annotations

import copy
import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import execute_order
from paper_order_router import apply_frontier_paper_guard
from settings import DEFAULT_SETTINGS
from storage import init_db
from strategy_reliability import apply_strategy_reliability, paper_context_loss_quarantine_record


def candidate(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "venue": "MEXC",
        "asset_surface": "frontier_spot",
        "market_surface": "frontier_spot",
        "inst_id": "MEXC:ABC-USDT",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "long_frontier_spot",
        "signal_key": "MEXC|frontier|long",
        "score": 88.0,
        "last": 100.0,
        "edge_bps_estimate": 20.0,
        "execution_feasibility": {"status": "standard"},
    }
    row.update(overrides)
    return row


def failing_stats(**overrides: object) -> dict[str, object]:
    stats: dict[str, object] = {
        "closed_count": 16,
        "expectancy_bps": -14.0,
        "win_rate": 0.38,
        "tail_average_bps": -42.0,
        "worst_bps": -95.0,
    }
    stats.update(overrides)
    return stats


class PaperContextLossQuarantineTests(unittest.TestCase):
    def test_quarantine_requires_all_loss_signature_components(self) -> None:
        blocked = paper_context_loss_quarantine_record(
            candidate(paper_context_loss_stats=failing_stats()), {"mode": "paper"}
        )
        self.assertTrue(blocked["quarantined"])
        self.assertEqual("mexc|frontier_spot|frontier_crypto_venue_map|long_frontier_spot", blocked["context_key"])

        tail_recovered = paper_context_loss_quarantine_record(
            candidate(paper_context_loss_stats=failing_stats(tail_average_bps=-5.0, worst_bps=-20.0)),
            {"mode": "paper"},
        )
        self.assertIsNone(tail_recovered)

    def test_context_is_removed_from_ranking_and_router_fill_path(self) -> None:
        rows, report = apply_strategy_reliability(
            [candidate(paper_context_loss_stats=failing_stats())], settings={"mode": "paper"}
        )
        blocked = rows[0]
        self.assertEqual(0.0, blocked["score"])
        self.assertTrue(blocked["paper_entry_blocked"])
        self.assertFalse(blocked["paper_rank_eligible"])
        self.assertEqual(1, report["summary"]["context_loss_quarantine_count"])

        guarded = apply_frontier_paper_guard(blocked, {"mode": "paper"})
        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual("paper_context_loss_quarantine", guarded["candidate_reject_detail"]["guard"])

    def test_rolling_closed_paper_outcomes_create_persisted_quarantine(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            payload = json.dumps(candidate())
            for index in range(16):
                pnl = 8.0 if index < 6 else -30.0
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type,
                        signal_key, base_score, learned_score, entry, exit, pnl_bps,
                        status, thesis, candidate_json, review_json,
                        close_measurement_status
                    ) values (?, ?, 'MEXC', 'MEXC:ABC-USDT', 'long_frontier_spot',
                              'frontier_crypto_venue_map', 'MEXC|frontier|long',
                              80, 80, 100, 99, ?, 'closed', 'test', ?, '{}', 'valid')
                    """,
                    (f"2026-08-01T00:{index:02d}:00+00:00", f"2026-08-01T01:{index:02d}:00+00:00", pnl, payload),
                )
            conn.commit()
            rows, _ = apply_strategy_reliability([candidate()], settings={"mode": "paper"}, conn=conn)
            state = conn.execute(
                "select status, baseline_closed_count from paper_context_quarantines"
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertEqual("active", state["status"])
        self.assertEqual(16, state["baseline_closed_count"])

    def test_recovery_requires_cooldown_and_new_paper_sample(self) -> None:
        active_state = {"status": "active", "cooldown_until": "2000-01-01T00:00:00+00:00"}
        base = candidate(
            paper_context_loss_stats=failing_stats(),
            paper_context_loss_quarantine_state=active_state,
        )
        still_blocked = paper_context_loss_quarantine_record(base, {"mode": "paper"})
        self.assertTrue(still_blocked["quarantined"])

        recovered = paper_context_loss_quarantine_record(
            {
                **base,
                "paper_context_recovery_stats": {
                    "closed_count": 8,
                    "expectancy_bps": 4.0,
                    "win_rate": 0.63,
                    "tail_average_bps": -8.0,
                },
            },
            {"mode": "paper"},
        )
        self.assertTrue(recovered["recovered"])
        self.assertFalse(recovered["quarantined"])

    def test_live_context_and_exploration_cannot_turn_it_into_a_fill(self) -> None:
        raw = candidate(paper_context_loss_stats=failing_stats())
        self.assertIsNone(paper_context_loss_quarantine_record(raw, {"mode": "live"}))
        self.assertFalse(
            apply_frontier_paper_guard(
                {**raw, "paper_context_loss_quarantine": {"paper_fill_allowed": False}},
                {"mode": "live"},
            ).get("shadow_filtered", False)
        )

        rows, _ = apply_strategy_reliability([raw], settings={"mode": "paper"})
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["paper_exploration"]["enabled"] = True
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            execution = execute_order(
                conn,
                rows[0],
                {"paper_allocation_multiplier": 1.0, "signal_key": "MEXC|frontier|long"},
                settings,
            )
        finally:
            conn.close()
        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_filtered", execution["order"]["status"])


if __name__ == "__main__":
    unittest.main()
