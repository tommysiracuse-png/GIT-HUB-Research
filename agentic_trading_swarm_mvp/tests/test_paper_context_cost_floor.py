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

from agent_review import review_candidate  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from paper_context_cost import (  # noqa: E402
    annotate_paper_context_cost,
    paper_context_cost_gate,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


def frontier_candidate(**overrides: object) -> dict:
    candidate = {
        "venue": "COINBASE",
        "inst_id": "COINBASE:BTC-USD",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "long_frontier_spot",
        "score": 80.0,
        "last": 100.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "gross_edge_bps_estimate": 40.0,
        "edge_bps_estimate": 18.0,
        "estimated_round_trip_cost_bps": 20.0,
        "liquidity_score": 0.9,
        "spread_bps": 3.0,
        "freshness_age_seconds": 10.0,
        "recent_volatility_bps": 10.0,
        "change_24h_pct": 1.0,
        "execution_feasibility": {"status": "standard", "legs": ["paper spot leg"]},
    }
    candidate.update(overrides)
    return candidate


class PaperContextCostFloorTests(unittest.TestCase):
    def test_gross_edge_must_clear_safety_adjusted_context_floor(self) -> None:
        gate = paper_context_cost_gate(
            frontier_candidate(gross_edge_bps_estimate=24.0),
            DEFAULT_SETTINGS,
        )

        self.assertTrue(gate["applicable"])
        self.assertFalse(gate["eligible"])
        self.assertGreater(gate["required_gross_edge_bps"], 24.0)
        self.assertEqual(gate["safety_multiplier"], 1.25)
        self.assertIn("gross_edge_does_not_clear_context_cost_floor", gate["reasons"])

        stricter = copy.deepcopy(DEFAULT_SETTINGS)
        stricter["paper_context_cost_floor"]["safety_multiplier"] = 2.0
        self.assertGreater(
            paper_context_cost_gate(frontier_candidate(), stricter)["required_gross_edge_bps"],
            paper_context_cost_gate(frontier_candidate(), DEFAULT_SETTINGS)["required_gross_edge_bps"],
        )

    def test_freshness_liquidity_volatility_and_complexity_raise_floor(self) -> None:
        healthy = paper_context_cost_gate(frontier_candidate(), DEFAULT_SETTINGS)
        poor = paper_context_cost_gate(
            frontier_candidate(
                gross_edge_bps_estimate=100.0,
                liquidity_score=0.1,
                freshness_age_seconds=90.0,
                recent_volatility_bps=100.0,
                execution_leg_count=2,
            ),
            DEFAULT_SETTINGS,
        )

        self.assertGreater(poor["context_cost_floor_bps"], healthy["context_cost_floor_bps"])
        self.assertGreater(poor["components_bps"]["freshness"], 0.0)
        self.assertGreater(poor["components_bps"]["liquidity"], healthy["components_bps"]["liquidity"])
        self.assertGreater(poor["components_bps"]["volatility"], healthy["components_bps"]["volatility"])
        self.assertEqual(poor["components_bps"]["complexity"], 4.0)
        self.assertTrue(poor["eligible"])
        self.assertLess(poor["score_multiplier"], healthy["score_multiplier"])

    def test_annotation_down_ranks_thin_stale_surface_without_mutation(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=100.0,
            liquidity_score=0.1,
            freshness_age_seconds=90.0,
        )

        annotated = annotate_paper_context_cost(candidate, DEFAULT_SETTINGS)

        self.assertEqual(candidate["score"], 80.0)
        self.assertEqual(annotated["score_before_context_cost"], 80.0)
        self.assertLess(annotated["score"], 80.0)
        self.assertIn("paper_context_cost_gate", annotated)

    def test_review_rejects_low_gross_edge_even_when_net_edge_minimum_passes(self) -> None:
        candidate = frontier_candidate(
            gross_edge_bps_estimate=24.0,
            edge_bps_estimate=18.0,
        )

        review = review_candidate(candidate, copy.deepcopy(DEFAULT_SETTINGS), {})

        self.assertEqual(review["decision"], "reject")
        self.assertTrue(
            any("paper context cost floor not cleared" in block for block in review["hard_blocks"])
        )
        self.assertFalse(review["paper_context_cost_gate"]["eligible"])

    def test_policy_is_paper_only_configurable_and_scope_limited(self) -> None:
        disabled = copy.deepcopy(DEFAULT_SETTINGS)
        disabled["paper_context_cost_floor"]["enabled"] = False
        candidate = frontier_candidate(gross_edge_bps_estimate=1.0)

        self.assertTrue(paper_context_cost_gate(candidate, disabled)["eligible"])
        unrelated = paper_context_cost_gate(
            {"trade_type": "perp_funding_basis", "gross_edge_bps_estimate": 1.0},
            DEFAULT_SETTINGS,
        )
        self.assertFalse(unrelated["applicable"])
        self.assertTrue(unrelated["eligible"])
        live_settings = copy.deepcopy(DEFAULT_SETTINGS)
        live_settings["mode"] = "live"
        live_gate = paper_context_cost_gate(candidate, live_settings)
        self.assertFalse(live_gate["enabled"])
        self.assertTrue(live_gate["eligible"])

    def test_fill_boundary_prioritizes_proxy_family_quarantine(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "YAHOO_PROXY",
            "inst_id": "EWZ",
            "trade_type": "global_proxy_momentum",
            "direction": "long_proxy",
            "score": 70.0,
            "last": 25.0,
            "gross_edge_bps_estimate": 5.0,
            "edge_bps_estimate": 3.0,
            "spread_bps": 3.0,
            "liquidity_score": 0.9,
            "provider_age_seconds": 10.0,
            "execution_feasibility": {"status": "standard"},
        }
        approved_review = {
            "decision": "approve_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 3.0,
            "feasibility_status": "standard",
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(conn, candidate, approved_review, DEFAULT_SETTINGS)

        self.assertFalse(result["paper_filled"])
        self.assertEqual(result["order"]["status"], "shadow_filtered")
        self.assertEqual(
            result["order"]["shadow_filter"]["reason"],
            "quarantined_family_decay",
        )
        self.assertEqual(
            result["order"]["shadow_filter"]["family_key"],
            "YAHOO_PROXY|global_proxy_momentum",
        )


if __name__ == "__main__":
    unittest.main()
