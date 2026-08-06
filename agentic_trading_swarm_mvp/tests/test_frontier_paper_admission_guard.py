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

from execution_engine import execute_order  # noqa: E402
from frontier_crypto_adapter import rank_frontier_paper_candidates  # noqa: E402
from paper_order_router import (  # noqa: E402
    FRONTIER_SHADOW_REASON,
    apply_frontier_paper_admission_guard,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


def candidate(**overrides: object) -> dict:
    row = {
        "venue": "COINBASE",
        "inst_id": "COINBASE:BTC-USD",
        "trade_type": "frontier_crypto_venue_map",
        "market_surface": "frontier_crypto_venue_map",
        "frontier_paper_admission_guard_applies": True,
        "signal_key": "COINBASE|frontier_crypto_venue_map|long_frontier_spot|standard",
        "direction": "long_frontier_spot",
        "last": 100.0,
        "score": 90.0,
        "route_status": "standard",
        "quality_status": "verified",
        "quality_action": "normal",
        "quality_score": 90.0,
        "edge_bps_estimate": 10.0,
        "gross_edge_bps_estimate": 30.0,
        "estimated_round_trip_cost_bps": 20.0,
        "anomaly_flags": [],
        "execution_feasibility": {"status": "standard", "route_status": "standard"},
    }
    row.update(overrides)
    return row


class FrontierPaperAdmissionGuardTests(unittest.TestCase):
    def test_guard_records_every_failed_admission_requirement(self) -> None:
        guarded = apply_frontier_paper_admission_guard(
            candidate(
                route_status="conditional",
                execution_feasibility={"status": "conditional", "route_status": "conditional"},
                quality_status="degraded",
                quality_action="shadow_only",
                quality_score=79.0,
                edge_bps_estimate=0.0,
                gross_edge_bps_estimate=24.0,
                anomaly_flags=[
                    "empty_book",
                    "invalid_best_prices",
                    "ticker_book_midpoint_mismatch",
                    "simulated_slippage_exceeds_edge",
                    "stale_book",
                    "depth_cliff",
                ],
            )
        )

        codes = {check["code"] for check in guarded["candidate_reject_detail"]["checks"]}
        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertEqual(FRONTIER_SHADOW_REASON, guarded["candidate_reject_reason"])
        self.assertTrue(
            {
                "route_status_not_standard",
                "quality_status_not_verified",
                "quality_action_not_normal",
                "quality_score_below_80",
                "non_positive_net_edge",
                "gross_edge_not_at_least_5bps_above_round_trip_cost",
                "empty_book",
                "invalid_best_prices",
                "ticker_book_midpoint_mismatch",
                "simulated_slippage_exceeds_edge",
                "stale_book",
                "depth_cliff",
            }.issubset(codes)
        )

    def test_ranked_shadow_candidate_remains_reportable(self) -> None:
        weak = candidate(gross_edge_bps_estimate=24.0)

        ranked = rank_frontier_paper_candidates([weak], copy.deepcopy(DEFAULT_SETTINGS))

        self.assertEqual([weak], ranked)
        self.assertTrue(weak["shadow_filtered"])
        self.assertEqual(FRONTIER_SHADOW_REASON, weak["candidate_reject_reason"])

    def test_exploration_boundary_does_not_fill_a_rejected_frontier_candidate(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        review = {
            "decision": "approve_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 0.0,
            "feasibility_status": "standard",
            "route_status": "standard",
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(conn, candidate(edge_bps_estimate=0.0), review, copy.deepcopy(DEFAULT_SETTINGS))

        self.assertFalse(result["paper_filled"])
        self.assertEqual("shadow_filtered", result["order"]["status"])
        self.assertEqual(FRONTIER_SHADOW_REASON, result["candidate"]["candidate_reject_reason"])


if __name__ == "__main__":
    unittest.main()
