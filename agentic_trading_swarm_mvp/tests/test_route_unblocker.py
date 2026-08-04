from __future__ import annotations

import copy
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agent_review  # noqa: E402
import paper_order_router  # noqa: E402
import route_resolver  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402


def settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


def base_candidate(**overrides: object) -> dict:
    candidate = {
        "venue": "GATE",
        "inst_id": "GATE:ABC_USDT",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "short_frontier_spot",
        "asset_class": "crypto_spot",
        "score": 80.0,
        "last": 1.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "edge_bps_estimate": 25.0,
        "liquidity_score": 0.8,
        "spread_bps": 2.0,
        "change_24h_pct": 1.0,
        "data_status": "reachable",
    }
    candidate.update(overrides)
    return candidate


class RouteUnblockerTests(unittest.TestCase):
    def test_inferred_proxy_does_not_bypass_spot_short_eligibility_gate(self) -> None:
        cfg = settings()
        enriched = route_resolver.enrich_candidate_with_route(base_candidate(), cfg)

        review = agent_review.review_candidate(enriched, cfg, {})

        self.assertEqual(review["decision"], "conditional_review")
        self.assertFalse(review["route_alternative_used"])
        self.assertEqual(review["effective_route_id"], "conditional_crypto_route_paper")
        self.assertTrue(
            any("paper route eligibility blocked" in block for block in review["hard_blocks"])
        )
        self.assertIn(
            "borrowable",
            enriched["paper_route_eligibility"]["missing_prerequisites"],
        )

        guarded = paper_order_router.apply_frontier_paper_guard(enriched, cfg)
        self.assertTrue(guarded.get("shadow_filtered", False))
        self.assertEqual(
            guarded["frontier_paper_guard"]["reason"],
            "paper_route_eligibility_blocked",
        )

    def test_prediction_market_blockers_use_research_paper_route_only(self) -> None:
        cfg = settings()
        candidate = base_candidate(
            venue="POLYMARKET",
            inst_id="poly:123",
            trade_type="prediction_market",
            direction="buy_yes_event",
            asset_class="prediction_markets",
            edge_bps_estimate=12.0,
        )
        enriched = route_resolver.enrich_candidate_with_route(candidate, cfg)

        review = agent_review.review_candidate(enriched, cfg, {})

        self.assertEqual(review["decision"], "approve_conditional_paper_trade")
        self.assertTrue(review["route_alternative_used"])
        self.assertEqual(review["effective_route_id"], "prediction_market_public_research_paper")
        self.assertEqual(review["route_alternative"]["status"], "paper_testable_research")
        self.assertEqual(review["paper_allocation_multiplier"], 0.1)
        self.assertIn("jurisdiction_eligibility", review["direct_missing_requirements"])


if __name__ == "__main__":
    unittest.main()
