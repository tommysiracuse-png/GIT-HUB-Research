import copy
import unittest

from src.route_resolver import (
    enrich_candidate_with_route,
    evaluate_route_intelligence,
    summarize_routes,
)
from src.settings import DEFAULT_SETTINGS


class TestPaperRouteExecutabilityGate(unittest.TestCase):
    def test_spot_short_requires_explicit_candidate_level_prerequisites(self):
        verdict = evaluate_route_intelligence(
            {
                "route_id": "spot_short",
                "surface": "spot",
                "direction": "short",
                "spot_inventory_held": False,
                "expected_edge_bps": 30.0,
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertFalse(verdict["eligible_for_scoring"])
        self.assertEqual("blocked_hard", verdict["route_decision"])
        self.assertEqual(
            {
                "paper_short_simulation_allowed",
                "borrowable",
                "borrow_cost_assumption",
                "margin_eligible",
            },
            set(verdict["missing_prerequisites"]),
        )

    def test_spot_short_passes_with_all_explicit_paper_fields(self):
        verdict = evaluate_route_intelligence(
            {
                "route_id": "spot_short",
                "surface": "spot",
                "direction": "short",
                "spot_inventory_held": False,
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "expected_edge_bps": 20.0,
                "estimated_fee_bps": 2.0,
                "estimated_slippage_bps": 1.0,
            }
        )

        self.assertTrue(verdict["route_eligible"])
        self.assertFalse(verdict["suppressed"])
        self.assertEqual("executable_standard", verdict["route_decision"])
        self.assertEqual(7.0, verdict["assumed_route_cost_bps"])

    def test_held_spot_inventory_does_not_require_borrow_fields(self):
        verdict = evaluate_route_intelligence(
            {
                "route_id": "spot_inventory_sale",
                "surface": "spot",
                "direction": "short",
                "spot_inventory_held": True,
            }
        )

        self.assertFalse(verdict["spot_short_required"])
        self.assertTrue(verdict["route_eligible"])

    def test_basis_candidate_requires_hedge_and_leg_mapping(self):
        verdict = evaluate_route_intelligence(
            {
                "trade_type": "perp_funding_basis",
                "direction": "short_perp_long_spot",
                "expected_edge_bps": 18.0,
            }
        )

        self.assertTrue(verdict["hedged_structure_required"])
        self.assertEqual(
            {
                "hedge_venue",
                "hedge_instrument",
                "fee_model",
                "paper_leg_mapping_valid",
            },
            set(verdict["missing_prerequisites"]),
        )

    def test_basis_candidate_passes_with_valid_explicit_leg_contract(self):
        verdict = evaluate_route_intelligence(
            {
                "trade_type": "perp_funding_basis",
                "direction": "short_perp_long_spot",
                "hedge_venue": "OKX_SPOT",
                "hedge_instrument": "BTC-USDT",
                "fee_model": "paper_conservative_v1",
                "paper_leg_mapping_valid": True,
                "expected_edge_bps": 18.0,
                "estimated_fee_bps": 3.0,
                "estimated_slippage_bps": 2.0,
                "funding_drag_bps": 1.0,
            }
        )

        self.assertTrue(verdict["route_eligible"])
        self.assertEqual([], verdict["missing_prerequisites"])

    def test_costs_larger_than_edge_block_scoring(self):
        verdict = evaluate_route_intelligence(
            {
                "trade_type": "funding_capture",
                "hedge_venue": "OKX_SPOT",
                "hedge_instrument": "BTC-USDT",
                "fee_model": "paper_conservative_v1",
                "paper_leg_mapping_valid": True,
                "expected_edge_bps": 5.0,
                "estimated_fee_bps": 2.0,
                "estimated_slippage_bps": 3.0,
                "funding_drag_bps": 1.0,
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertIn("expected_edge_below_route_costs", verdict["blocker_reasons"])
        self.assertIn("positive_edge_after_route_costs", verdict["missing_prerequisites"])

    def test_explicit_proxy_does_not_bypass_cost_consistency(self):
        verdict = evaluate_route_intelligence(
            {
                "route_id": "spot_short",
                "direction": "short",
                "surface": "spot",
                "route_requirements": {
                    "borrow_required": True,
                    "proxy_allowed": True,
                    "paper_proxy_id": "perp_paper_proxy",
                },
                "expected_edge_bps": 2.0,
                "estimated_fee_bps": 3.0,
            }
        )

        self.assertTrue(verdict["proxy_used"])
        self.assertTrue(verdict["suppressed"])
        self.assertEqual("blocked_hard", verdict["route_decision"])
        self.assertEqual(
            ["positive_edge_after_route_costs"],
            verdict["missing_prerequisites"],
        )

    def test_enrichment_clamps_score_and_reports_exact_route_gaps(self):
        candidate = {
            "venue": "GATE",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "short_frontier_spot",
            "asset_class": "crypto_spot",
            "data_status": "reachable",
            "score": 88.0,
        }

        enriched = enrich_candidate_with_route(
            candidate,
            copy.deepcopy(DEFAULT_SETTINGS),
        )
        summary = summarize_routes([enriched])

        self.assertEqual(0.0, enriched["score"])
        self.assertEqual(88.0, enriched["pre_route_eligibility_score"])
        self.assertTrue(enriched["paper_entry_blocked"])
        self.assertIn("borrowable", enriched["paper_route_eligibility"]["missing_prerequisites"])
        self.assertEqual(
            1,
            summary["by_paper_route_eligibility"]["blocked_hard"],
        )
        self.assertEqual(
            1,
            summary["by_paper_route_eligibility_blocker"]["spot_borrow_missing"],
        )


if __name__ == "__main__":
    unittest.main()
