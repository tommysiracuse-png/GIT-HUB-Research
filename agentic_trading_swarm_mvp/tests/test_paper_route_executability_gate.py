import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.route_resolver import (
    enrich_candidate_with_route,
    evaluate_route_intelligence,
    summarize_routes,
)
from src.settings import DEFAULT_SETTINGS
from strategy_reliability import _annotate


class TestPaperRouteExecutabilityGate(unittest.TestCase):
    def test_side_instrument_and_strategy_tags_infer_spot_short_requirements(self):
        verdict = evaluate_route_intelligence(
            {
                "instrument": "ALT-USDT spot",
                "side": "sell",
                "strategy_tags": ["conditional", "margin_required"],
                "venue_capabilities": {
                    "supports_spot_short": False,
                    "supports_margin_spot": False,
                    "supports_borrow_check": False,
                },
            }
        )

        self.assertTrue(verdict["spot_short_required"])
        self.assertEqual(
            {
                "supports_spot_short",
                "supports_margin_spot",
                "supports_borrow_check",
            },
            set(verdict["required_capabilities"]),
        )
        self.assertEqual("infeasible_for_paper", verdict["feasibility_status"])

    def test_canonical_capabilities_fail_closed_with_auditable_states(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short_frontier_spot",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "venue_capabilities": {
                    "supports_spot_short": False,
                    "supports_margin_spot": None,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertEqual("infeasible_for_paper", verdict["feasibility_status"])
        self.assertEqual("blocked", verdict["execution_eligibility"])
        states = {
            check["capability"]: check["state"]
            for check in verdict["capability_checks"]
        }
        self.assertEqual("unsupported", states["supports_spot_short"])
        self.assertEqual("unknown", states["supports_margin_spot"])
        self.assertEqual("supported", states["supports_borrow_check"])

    def test_explicit_unknown_borrow_assumptions_pass_only_with_severe_penalty(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short_frontier_spot",
                "paper_short_simulation_allowed": True,
                "borrow_inventory_assumption": "fixed_conservative_inventory",
                "borrow_cost_assumption": {"bps": 25.0, "model": "paper_stress"},
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertFalse(verdict["suppressed"])
        self.assertEqual(
            "feasible_with_simulation_assumptions",
            verdict["feasibility_status"],
        )
        self.assertTrue(verdict["assumption_penalty_applied"])
        self.assertEqual(0.2, verdict["paper_score_multiplier"])
        self.assertEqual(
            {"borrow_inventory", "borrow_cost"},
            set(verdict["simulation_assumptions"]),
        )

    def test_supported_route_without_borrow_assumptions_remains_blocked(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short_frontier_spot",
                "paper_short_simulation_allowed": True,
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertIn("spot_borrow_missing", verdict["blocker_reasons"])
        self.assertIn("borrow_cost_assumption_missing", verdict["blocker_reasons"])
        self.assertEqual(0.0, verdict["paper_score_multiplier"])

    def test_canonical_basis_path_veto_wins_over_legacy_support_flag(self):
        verdict = evaluate_route_intelligence(
            {
                "trade_type": "perp_funding_basis",
                "direction": "short_perp_long_spot",
                "hedge_venue": "OKX_SPOT",
                "hedge_instrument": "BTC-USDT",
                "fee_model": "paper_conservative_v1",
                "paper_leg_mapping_valid": True,
                "venue_capabilities": {
                    "supports_basis_path": False,
                    "supports_basis_carry": True,
                    "supports_spot_long": True,
                    "supports_perpetuals": True,
                },
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertIn(
            "venue_synthetic_carry_capability_unconfirmed",
            verdict["blocker_reasons"],
        )

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
                "venue_capabilities",
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
                "venue_capabilities": {"paper_route_feasible": True},
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
                "venue_capabilities",
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
                "venue_capabilities": {"paper_route_feasible": True},
                "expected_edge_bps": 18.0,
                "estimated_fee_bps": 3.0,
                "estimated_slippage_bps": 2.0,
                "funding_drag_bps": 1.0,
            }
        )

        self.assertTrue(verdict["route_eligible"])
        self.assertEqual([], verdict["missing_prerequisites"])

    def test_candidate_flags_cannot_replace_missing_venue_capability_metadata(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertFalse(verdict["venue_capability_metadata_present"])
        self.assertIn("venue_capabilities", verdict["missing_prerequisites"])
        self.assertIn(
            "venue_capability_metadata_missing",
            verdict["blocker_reasons"],
        )

    def test_detailed_capability_veto_overrides_aggregate_feasible_flag(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "venue_capabilities": {
                    "paper_route_feasible": True,
                    "borrow_supported": False,
                },
            }
        )

        self.assertTrue(verdict["suppressed"])
        self.assertIn(
            "venue_borrow_capability_unconfirmed",
            verdict["blocker_reasons"],
        )

    def test_unsupported_venue_short_capabilities_override_loose_candidate_flags(self):
        verdict = evaluate_route_intelligence(
            {
                "surface": "spot",
                "direction": "short",
                "paper_short_simulation_allowed": True,
                "borrowable": True,
                "borrow_cost_bps": 4.0,
                "margin_eligible": True,
                "venue_capabilities": {
                    "supports_spot_short_margin": False,
                    "margin_supported": False,
                    "borrow_supported": False,
                },
            }
        )

        self.assertTrue(verdict["venue_capability_metadata_present"])
        self.assertTrue(verdict["suppressed"])
        self.assertIn(
            "venue_spot_short_capability_unconfirmed",
            verdict["blocker_reasons"],
        )
        self.assertIn("venue_margin_capability_unconfirmed", verdict["blocker_reasons"])
        self.assertIn("venue_borrow_capability_unconfirmed", verdict["blocker_reasons"])

    def test_carry_route_requires_supported_spot_perp_and_carry_capabilities(self):
        candidate = {
            "trade_type": "perp_funding_basis",
            "direction": "short_perp_long_spot",
            "hedge_venue": "OKX_SPOT",
            "hedge_instrument": "BTC-USDT",
            "fee_model": "paper_conservative_v1",
            "paper_leg_mapping_valid": True,
            "venue_capabilities": {
                "supports_spot_long": True,
                "supports_perpetuals": True,
                "supports_basis_carry": False,
            },
        }

        blocked = evaluate_route_intelligence(candidate)
        self.assertTrue(blocked["suppressed"])
        self.assertIn(
            "venue_synthetic_carry_capability_unconfirmed",
            blocked["blocker_reasons"],
        )

        candidate["venue_capabilities"]["supports_basis_carry"] = True
        allowed = evaluate_route_intelligence(candidate)
        self.assertFalse(allowed["suppressed"])
        self.assertEqual("executable_standard", allowed["route_decision"])

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
                "venue_capabilities": {"paper_route_feasible": True},
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

    def test_enrichment_down_ranks_and_reports_exact_route_gaps(self):
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

        self.assertGreater(enriched["score"], 0.0)
        self.assertLess(enriched["score"], 88.0)
        self.assertFalse(enriched.get("paper_entry_blocked", False))
        self.assertFalse(enriched["paper_route_eligibility"]["suppressed"])
        self.assertIn("borrowable", enriched["paper_route_eligibility"]["route_diagnostic_reasons"])
        self.assertEqual(
            1,
            summary["by_paper_route_eligibility"]["paper_observation"],
        )
        self.assertNotIn(
            "spot_borrow_missing",
            summary["by_paper_route_eligibility_blocker"],
        )

    def test_venue_registry_blocks_short_even_when_account_borrow_is_enabled(self):
        cfg = copy.deepcopy(DEFAULT_SETTINGS)
        cfg["account_capabilities"]["spot_borrow"] = True
        candidate = {
            "venue": "GATE",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "short_frontier_spot",
            "asset_class": "crypto_spot",
            "data_status": "reachable",
            "score": 88.0,
            "paper_short_simulation_allowed": True,
            "borrowable": True,
            "borrow_cost_bps": 4.0,
            "margin_eligible": True,
        }

        enriched = enrich_candidate_with_route(candidate, cfg)

        self.assertEqual("standard", enriched["route_status"])
        self.assertEqual(
            "paper_route_intelligence.crypto_venues",
            enriched["venue_capability_source"],
        )
        self.assertFalse(enriched["paper_route_eligibility"]["suppressed"])
        self.assertGreater(enriched["score"], 0.0)
        self.assertIn(
            "venue_spot_short_capability_unconfirmed",
            enriched["paper_route_eligibility"]["route_diagnostic_reasons"],
        )

    def test_candidate_capability_flags_cannot_override_maintained_venue_veto(self):
        candidate = {
            "venue": "OKX_SPOT",
            "trade_type": "frontier_crypto_venue_map",
            "direction": "short_frontier_spot",
            "asset_class": "crypto_spot",
            "score": 80.0,
            "paper_short_simulation_allowed": True,
            "borrowable": True,
            "borrow_cost_bps": 4.0,
            "margin_eligible": True,
            "venue_capabilities": {
                "supports_spot_short": True,
                "supports_margin_spot": True,
                "supports_borrow_check": True,
            },
        }

        enriched = enrich_candidate_with_route(
            candidate,
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        capabilities = enriched["paper_route_eligibility"]["venue_capabilities"]
        self.assertFalse(capabilities["supports_spot_short"])
        self.assertFalse(capabilities["supports_margin_spot"])
        self.assertFalse(capabilities["supports_borrow_check"])
        self.assertEqual("eligible", enriched["execution_eligibility"])
        self.assertEqual("feasible_with_route_diagnostics", enriched["paper_feasibility_status"])

    def test_borrow_cost_assumption_is_included_in_route_cost_gate(self):
        verdict = evaluate_route_intelligence(
            {
                "venue": "PAPER_SIM_VENUE",
                "surface": "spot",
                "direction": "short_frontier_spot",
                "paper_short_simulation_allowed": True,
                "borrow_inventory_assumption": "fixed_conservative_inventory",
                "borrow_cost_assumption": {"bps": 25.0, "model": "paper_stress"},
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
                "expected_edge_bps": 10.0,
            }
        )

        self.assertEqual(25.0, verdict["cost_breakdown_bps"]["borrow"])
        self.assertEqual(25.0, verdict["assumed_route_cost_bps"])
        self.assertTrue(verdict["suppressed"])
        self.assertIn("expected_edge_below_route_costs", verdict["blocker_reasons"])

    def test_spot_candidate_uses_instrument_specific_venue_profile(self):
        enriched = enrich_candidate_with_route(
            {
                "venue": "OKX",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "asset_class": "crypto_spot",
                "data_status": "reachable",
                "score": 88.0,
            },
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        capabilities = enriched["paper_route_eligibility"]["venue_capabilities"]
        self.assertEqual("OKX_SPOT", capabilities["capability_profile"])
        self.assertFalse(capabilities["supports_spot_short"])
        self.assertFalse(capabilities["supports_margin_spot"])
        self.assertFalse(capabilities["supports_borrow_check"])
        self.assertFalse(capabilities["supports_basis_path"])

    def test_enrichment_applies_assumption_penalty_to_score_and_allocation(self):
        enriched = enrich_candidate_with_route(
            {
                "venue": "PAPER_SIM_VENUE",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "asset_class": "crypto_spot",
                "data_status": "reachable",
                "score": 80.0,
                "paper_short_simulation_allowed": True,
                "borrow_inventory_assumption": "fixed_conservative_inventory",
                "borrow_cost_assumption": {"bps": 25.0, "model": "paper_stress"},
                "venue_capabilities": {
                    "supports_spot_short": True,
                    "supports_margin_spot": True,
                    "supports_borrow_check": True,
                },
            },
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        self.assertGreater(enriched["score"], 0.0)
        self.assertLess(enriched["score"], 80.0)
        self.assertEqual("eligible", enriched["execution_eligibility"])
        self.assertFalse(enriched.get("paper_entry_blocked", False))

    def test_reliability_scoring_keeps_conditional_short_as_paper_diagnostic(self):
        candidate = enrich_candidate_with_route(
            {
                "venue": "GATE",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "short_frontier_spot",
                "asset_class": "crypto_spot",
                "data_status": "reachable",
                "score": 88.0,
            },
            copy.deepcopy(DEFAULT_SETTINGS),
        )

        reliability = _annotate(
            candidate,
            profile="test_positive_reliability",
            action="would_raise_score_without_route_gate",
            reasons=["positive_alpha_evidence"],
            score_delta=25.0,
            allocation_multiplier=1.0,
            protect=True,
        )

        self.assertGreater(candidate["score"], 0.0)
        self.assertFalse(candidate.get("paper_entry_blocked", False))
        self.assertNotIn("route_eligibility_enforced", reliability)
        self.assertEqual(25.0, reliability["score_delta"])
        self.assertNotIn("paper_route_eligibility_blocked", reliability["reasons"])


if __name__ == "__main__":
    unittest.main()
