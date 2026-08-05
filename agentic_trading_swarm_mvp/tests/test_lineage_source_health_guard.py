from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_order_router import apply_frontier_paper_guard
from settings import DEFAULT_SETTINGS
from storage import init_db
from strategy_lab import generate_strategy_lab_candidates
from strategy_reliability import (
    apply_strategy_reliability,
    paper_lineage_source_health_record,
    paper_source_veto_record,
)


def candidate(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "venue": "OKX_SPOT",
        "inst_id": "OKX_SPOT:ABC-USDT",
        "trade_type": "frontier_crypto_venue_map",
        "market_surface": "frontier_crypto_venue_map",
        "direction": "long_frontier_spot",
        "signal_key": "CHILD|frontier|long",
        "score": 80.0,
        "edge_bps_estimate": 12.0,
        "gross_edge_bps_estimate": 36.0,
        "estimated_round_trip_cost_bps": 20.0,
        "quality_action": "verified",
        "anomaly_flags": [],
        "execution_feasibility": {"status": "standard"},
        "paper_filled": True,
        "status": "paper_filled",
    }
    row.update(overrides)
    return row


def negative_health(*, closed_count: int, expectancy: float = -7.0) -> dict[str, object]:
    return {
        "source_signal_key": "PARENT|momentum|long",
        "closed_count": closed_count,
        "after_cost_expectancy_bps": expectancy,
        "win_rate": 0.3,
        "cost_basis": "realized_paper_pnl_bps",
    }


class LineageSourceHealthGuardTests(unittest.TestCase):
    def test_persistent_negative_parent_edge_is_quarantined_before_sorting(self) -> None:
        degraded = candidate(
            inst_id="OKX_SPOT:DEGRADED-USDT",
            score=99.0,
            parent_signal_health=negative_health(closed_count=12),
        )
        healthy = candidate(inst_id="OKX_SPOT:HEALTHY-USDT", score=60.0)

        rows, report = apply_strategy_reliability([degraded, healthy])

        by_inst = {row["inst_id"]: row for row in rows}
        guarded = by_inst["OKX_SPOT:DEGRADED-USDT"]
        self.assertEqual(0.0, guarded["score"])
        self.assertTrue(guarded["paper_entry_blocked"])
        self.assertFalse(guarded["paper_rank_eligible"])
        self.assertEqual(
            "paper_lineage_source_negative_edge_quarantine",
            guarded["paper_lineage_source_health"]["reason"],
        )
        self.assertEqual("OKX_SPOT:HEALTHY-USDT", rows[0]["inst_id"])
        self.assertEqual(1, report["summary"]["lineage_source_health_guard_count"])

    def test_early_negative_parent_edge_gets_bounded_rank_penalty(self) -> None:
        degraded = candidate(parent_signal_health=negative_health(closed_count=4))

        rows, _ = apply_strategy_reliability([degraded])

        self.assertEqual(40.0, rows[0]["score"])
        self.assertFalse(rows[0].get("paper_entry_blocked", False))
        self.assertTrue(rows[0]["paper_rank_eligible"])
        self.assertFalse(rows[0]["promotion_eligible"])
        self.assertEqual("penalize", rows[0]["paper_lineage_source_health"]["action"])

    def test_explicit_lineage_hydrates_parent_health_from_paper_stats(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            conn.execute(
                """
                insert into signal_stats(
                    signal_key, closed_count, wins, avg_pnl_bps,
                    win_rate, score_adjustment, updated_at
                ) values (?, 14, 3, -9.0, 0.214, -5.0, '2026-08-04T00:00:00+00:00')
                """,
                ("PARENT|momentum|long",),
            )
            conn.commit()
            rows, _ = apply_strategy_reliability(
                [candidate(strategy_lab_source_signal_key="PARENT|momentum|long")],
                conn=conn,
            )
        finally:
            conn.close()

        review = rows[0]["paper_lineage_source_health"]
        self.assertEqual("persisted_paper_signal_stats", review["source_health"]["evidence_source"])
        self.assertEqual(-9.0, review["source_health"]["after_cost_expectancy_bps"])
        self.assertTrue(rows[0]["paper_entry_blocked"])

    def test_paper_router_refuses_persistent_negative_source_descendant(self) -> None:
        guarded = apply_frontier_paper_guard(
            candidate(parent_signal_health=negative_health(closed_count=20))
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertEqual(
            "paper_lineage_source_health",
            guarded["candidate_reject_detail"]["guard"],
        )

    def test_guard_is_unreachable_from_live_context(self) -> None:
        review = paper_lineage_source_health_record(
            candidate(parent_signal_health=negative_health(closed_count=20)),
            {"mode": "live"},
        )

        self.assertIsNone(review)

    def test_current_parent_stats_override_stale_static_recovery_packet(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        passing_window = {
            "sample_count": 12,
            "after_cost_expectancy_bps": 1.0,
            "freshness_pass_rate": 0.95,
            "execution_quality_pass_rate": 0.95,
        }
        settings["strategy_lab"]["yahoo_proxy_momentum_source_veto"]["recovery_evidence"] = {
            "source_family": {"windows": [passing_window] * 3},
            "immediate_descendants": {"windows": [passing_window] * 3},
        }
        row = candidate(
            market_key="YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            parent_signal_health=negative_health(closed_count=25, expectancy=-11.0),
        )
        self.assertIsNone(paper_source_veto_record(row, settings))

        rows, _ = apply_strategy_reliability([row], settings)

        self.assertTrue(rows[0]["paper_entry_blocked"])
        self.assertEqual(
            "paper_lineage_source_negative_edge_quarantine",
            rows[0]["paper_lineage_source_health"]["reason"],
        )

    def test_strategy_lab_keeps_negative_parent_as_exploration_diagnostic(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["allow_live_trading"] = False
        try:
            generated, report = generate_strategy_lab_candidates(
                conn,
                settings,
                [candidate(parent_signal_health=negative_health(closed_count=20))],
            )
        finally:
            conn.close()

        self.assertEqual([], generated)
        self.assertEqual(1, report["lineage_source_health_guarded_candidate_count"])
        self.assertEqual(1, report["route_eligible_source_candidate_count"])

    def test_rejected_cooldown_descendant_is_in_static_yahoo_lineage(self) -> None:
        veto = paper_source_veto_record(
            {
                "parent_strategy_lab_id": (
                    "tighten_entry_confirmation_and_add_paper_only_cooldown_65825268_child_v2"
                )
            },
            {"mode": "paper"},
        )

        self.assertIsNotNone(veto)
        self.assertEqual("strategy_lab_name_prefix", veto["matched_on"]["type"])


if __name__ == "__main__":
    unittest.main()
