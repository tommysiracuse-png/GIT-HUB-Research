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
    FRONTIER_PAPER_ADMISSION_REASON_PREFIX,
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
        self.assertEqual("shadow_only", guarded["paper_action"])
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:data_quality+cost+route_feasibility",
            guarded["candidate_reject_reason"],
        )
        self.assertTrue(
            {
                "quality_action_shadow_only",
                "quality_status_not_verified",
                "quality_score_below_minimum",
                "edge_bps_estimate_not_positive",
                "simulated_slippage_exceeds_edge",
                "route_status_not_standard",
                "empty_book",
                "invalid_best_prices",
                "ticker_book_midpoint_mismatch",
                "stale_book",
                "depth_cliff",
            }.issubset(codes)
        )
        self.assertEqual(guarded["candidate_reject_reason"], guarded["shadow_reason"])
        self.assertFalse(guarded["paper_score_eligible"])
        self.assertEqual(0.0, guarded["paper_score_multiplier"])
        self.assertFalse(guarded["frontier_paper_admission"]["admitted"])
        self.assertEqual(
            ["data_quality", "cost", "route_feasibility"],
            guarded["frontier_paper_admission"]["failed_categories"],
        )

    def test_explicit_anomaly_failure_is_shadowed(self) -> None:
        guarded = apply_frontier_paper_admission_guard(
            candidate(quality_action="conditional", anomaly_flags=["empty_book"])
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertEqual("shadow_only", guarded["paper_action"])
        self.assertEqual(
            {"empty_book"},
            {check["code"] for check in guarded["candidate_reject_detail"]["checks"]},
        )
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:data_quality",
            guarded["candidate_reject_reason"],
        )

    def test_exactly_five_bps_of_gross_cost_headroom_is_eligible(self) -> None:
        guarded = apply_frontier_paper_admission_guard(
            candidate(gross_edge_bps_estimate=25.0, estimated_round_trip_cost_bps=20.0)
        )

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertTrue(guarded["frontier_paper_admission"]["admitted"])

    def test_missing_optional_scanner_fields_do_not_shadow_an_otherwise_valid_candidate(self) -> None:
        guarded = apply_frontier_paper_admission_guard(
            candidate(
                quality_status=None,
                quality_action=None,
                gross_edge_bps_estimate=None,
                estimated_round_trip_cost_bps=None,
                anomaly_flags=None,
            )
        )

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertIsNone(guarded.get("candidate_reject_reason"))

    def test_ranked_shadow_candidate_remains_reportable(self) -> None:
        weak = candidate(quality_score=79.0)

        ranked = rank_frontier_paper_candidates([weak], copy.deepcopy(DEFAULT_SETTINGS))

        self.assertEqual([weak], ranked)
        self.assertTrue(weak["shadow_filtered"])
        self.assertEqual("shadow_only", weak["candidate_status"])
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:data_quality",
            weak["candidate_reject_reason"],
        )
        self.assertLessEqual(float(weak["score"]), 59.999)

    def test_verified_standard_route_positive_net_candidate_is_not_suppressed(self) -> None:
        guarded = apply_frontier_paper_admission_guard(
            candidate(
                quality_status="verified",
                quality_action="normal",
                quality_score=85.0,
                gross_edge_bps_estimate=24.0,
                estimated_round_trip_cost_bps=20.0,
                edge_bps_estimate=4.0,
                route_status="standard",
                anomaly_flags=[],
            )
        )

        self.assertFalse(guarded.get("shadow_filtered", False))
        self.assertIsNone(guarded.get("candidate_reject_reason"))

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
        self.assertEqual("shadow_only", result["order"]["status"])
        self.assertIsNone(result["order_id"])
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:cost",
            result["candidate"]["candidate_reject_reason"],
        )
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:cost",
            result["candidate"]["shadow_reason"],
        )
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:cost",
            observation[0],
        )


if __name__ == "__main__":
    unittest.main()
