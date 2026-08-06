import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_crypto_adapter import (  # noqa: E402
    paper_only_conditional_short_route_feasibility_gate,
    paper_only_conditional_short_route_requirements,
    paper_only_frontier_score_adjustment,
)


class FrontierRouteRequirementsTests(unittest.TestCase):
    def test_known_supported_route_requirements_are_exposed(self):
        requirements = paper_only_conditional_short_route_requirements(venue="gate_spot")
        self.assertEqual(requirements["venue_key"], "GATE")
        self.assertEqual(requirements["support_status"], "supported")
        self.assertTrue(requirements["supports_spot_short"])
        self.assertTrue(requirements["requires_margin_permission"])
        self.assertTrue(requirements["requires_borrow_check"])
        self.assertEqual(requirements["api_route_hint"], "spot_margin")

    def test_unknown_route_requirements_remain_reportable(self):
        requirements = paper_only_conditional_short_route_requirements(venue="bitso")
        self.assertEqual(requirements["support_status"], "unknown")
        self.assertIsNone(requirements["supports_spot_short"])
        self.assertIn("support_unknown", requirements["notes"])

    def test_unsupported_conditional_short_is_shadow_ranked_without_entry_block(self):
        gate = paper_only_conditional_short_route_feasibility_gate(
            venue="BINANCE_US_SPOT",
            direction="short",
            context_stats={
                "conditional": True,
                "context_key": "feasibility:conditional",
            },
        )
        self.assertTrue(gate["enabled"])
        self.assertTrue(gate["applied"])
        self.assertFalse(gate["allow"])
        self.assertTrue(gate["suppressed"])
        self.assertEqual(gate["score_multiplier"], 0.0)
        self.assertEqual(gate["route_feasibility_reason"], "conditional_short_paper_metadata_missing")
        self.assertFalse(gate["active_scoring_eligible"])
        self.assertTrue(gate["shadow_label"])
        self.assertTrue(gate["paper_ineligible"])
        self.assertIn("symbol_support_missing", gate["paper_rationale_codes"])
        self.assertIn("conditional_order_route_support_missing", gate["paper_rationale_codes"])

    def test_score_adjustment_fail_closes_when_route_prerequisites_are_missing(self):
        adjustment = paper_only_frontier_score_adjustment(
            venue="BITSO",
            direction="short_frontier_spot",
            context_stats={
                "closed_trade_count": 10,
                "recent_expectancy_bps": 6.0,
                "conditional": True,
                "trade_type": "frontier_crypto_venue_map",
                "context_key": "feasibility:conditional",
                "cross_market_divergence_bps": 2.0,
                "cross_market_trigger_bps": 1.0,
                "source_a_freshness_ms": 25.0,
                "source_b_freshness_ms": 25.0,
            },
            registry={
                "BITSO_SHORT_FRONTIER_SPOT": {
                    "enabled": True,
                    "min_closed_trades": 1,
                    "min_expectancy_bps": -999.0,
                }
            },
            route_feasibility_policy={
                "unknown_multiplier": 0.8,
            },
        )
        self.assertFalse(adjustment["allow"])
        self.assertTrue(adjustment["suppressed"])
        self.assertAlmostEqual(adjustment["score_multiplier"], 0.0)
        self.assertEqual(
            adjustment["route_feasibility_gate"]["route_requirements"]["support_status"],
            "unknown",
        )
        self.assertFalse(adjustment["active_scoring_eligible"])
        self.assertTrue(adjustment["route_feasibility_gate"]["shadow_label"])
        self.assertEqual(
            adjustment["route_feasibility_gate"]["reason"],
            "conditional_short_paper_metadata_missing",
        )
        self.assertEqual(
            adjustment["route_feasibility_reason"],
            "conditional_short_paper_metadata_missing",
        )
        self.assertTrue(adjustment["route_feasibility_gate"]["paper_ineligible"])

    def test_non_conditional_short_context_is_neutral(self):
        gate = paper_only_conditional_short_route_feasibility_gate(
            venue="BITSO",
            direction="short",
            context_stats={"context_key": "feasibility:standard"},
        )
        self.assertTrue(gate["enabled"])
        self.assertFalse(gate["applied"])
        self.assertTrue(gate["allow"])
        self.assertFalse(gate["suppressed"])
        self.assertEqual(gate["score_multiplier"], 1.0)

    def test_supported_venue_short_route_exception_remains_active(self):
        gate = paper_only_conditional_short_route_feasibility_gate(
            venue="GATE",
            direction="short_frontier_spot",
            context_stats={
                "execution_feasibility": {"status": "conditional"},
                "trade_type": "frontier_crypto_venue_map",
                "borrow_confirmed": True,
                "borrow_cost_model_present": True,
                "margin_eligible": True,
                "fees_modeled": True,
                "symbol_supported": True,
                "supports_conditional_orders": True,
            },
        )

        self.assertTrue(gate["applied"])
        self.assertTrue(gate["active_scoring_eligible"])
        self.assertFalse(gate["shadow_label"])
        self.assertFalse(gate["paper_ineligible"])
        self.assertEqual(
            gate["route_feasibility_reason"],
            "explicit_borrow_ok",
        )

    def test_verified_standard_short_route_remains_active_without_explicit_borrow_flag(self):
        gate = paper_only_conditional_short_route_feasibility_gate(
            venue="GATE",
            direction="short_frontier_spot",
            context_stats={
                "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                "trade_type": "frontier_crypto_venue_map",
                "paper_route_registry": {"support_status": "supported"},
                "margin_eligible": True,
                "fees_modeled": True,
                "symbol_supported": True,
                "supports_conditional_orders": True,
                "paper_route_requirement_report": {
                    "route_requirements": {
                        "route_requirement_checklist": {
                            "shortable_inventory_declared": True,
                            "borrow_cost_model_present": True,
                            "venue_supports_margin_or_equivalent": True,
                            "fees_modeled": True,
                        }
                    }
                },
            },
        )

        self.assertTrue(gate["applied"])
        self.assertTrue(gate["active_scoring_eligible"])
        self.assertFalse(gate["shadow_label"])
        self.assertFalse(gate["paper_ineligible"])
        self.assertFalse(gate["explicit_borrow_ok"])
        self.assertTrue(gate["verified_standard_route"])
        self.assertEqual(
            gate["route_feasibility_reason"],
            "verified_standard_short_route",
        )

    def test_generic_supported_route_without_verified_standard_path_is_shadow_only(self):
        gate = paper_only_conditional_short_route_feasibility_gate(
            venue="GATE",
            direction="short_frontier_spot",
            context_stats={
                "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                "trade_type": "frontier_crypto_venue_map",
                "margin_eligible": True,
                "fees_modeled": True,
                "symbol_supported": True,
                "supports_conditional_orders": True,
                "paper_route_requirement_report": {
                    "route_requirements": {
                        "route_requirement_checklist": {
                            "shortable_inventory_declared": True,
                            "borrow_cost_model_present": True,
                            "venue_supports_margin_or_equivalent": True,
                            "fees_modeled": True,
                        }
                    }
                },
            },
        )

        self.assertTrue(gate["applied"])
        self.assertTrue(gate["allow"])
        self.assertFalse(gate["suppressed"])
        self.assertGreater(gate["score_multiplier"], 0.0)
        self.assertLess(gate["score_multiplier"], 0.15)
        self.assertFalse(gate["active_scoring_eligible"])
        self.assertTrue(gate["shadow_label"])
        self.assertFalse(gate["paper_ineligible"])
        self.assertFalse(gate["explicit_borrow_ok"])
        self.assertFalse(gate["verified_standard_route"])
        self.assertEqual(
            gate["route_feasibility_reason"],
            "conditional_short_unverified_route",
        )


if __name__ == "__main__":
    unittest.main()
