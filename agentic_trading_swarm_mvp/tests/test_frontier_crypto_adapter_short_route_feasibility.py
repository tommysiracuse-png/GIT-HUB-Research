import unittest

from src.frontier_crypto_adapter import (
    paper_only_crypto_conditional_spot_short_feasibility_gate,
    paper_only_frontier_short_route_profile_gate,
)


class PaperOnlyCryptoConditionalSpotShortFeasibilityGateTests(unittest.TestCase):
    def test_not_applicable_for_non_conditional_spot_short_context(self):
        result = paper_only_crypto_conditional_spot_short_feasibility_gate(
            {
                "asset_class": "crypto",
                "market_type": "spot",
                "side": "long",
                "trigger_type": "conditional",
            }
        )

        self.assertFalse(result["applies"])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "not_applicable")

    def test_rejects_infeasible_conditional_spot_short_profile(self):
        result = paper_only_crypto_conditional_spot_short_feasibility_gate(
            {
                "asset_class": "crypto",
                "market_type": "spot",
                "side": "short",
                "trigger_type": "conditional",
                "venue_shorting_supported": False,
                "instrument_margin_shortable": False,
                "borrow_model_available": False,
                "route_lifecycle": ["open", "hold"],
                "estimated_fee_bps": None,
                "estimated_borrow_bps": None,
            }
        )

        self.assertTrue(result["applies"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "paper_short_route_infeasible")
        self.assertIn("venue_shorting_supported", result["failed_checks"])
        self.assertIn("instrument_shortable_or_borrow_model", result["failed_checks"])
        self.assertIn("open_hold_cover_lifecycle", result["failed_checks"])
        self.assertIn("estimated_fees_present", result["failed_checks"])
        self.assertIn("borrow_assumptions_present", result["failed_checks"])

    def test_accepts_supported_conditional_spot_short_profile(self):
        result = paper_only_crypto_conditional_spot_short_feasibility_gate(
            {
                "asset_class": "crypto",
                "market_type": "spot",
                "side": "short",
                "trigger_type": "conditional",
                "venue_shorting_supported": True,
                "instrument_margin_shortable": True,
                "route_lifecycle": "open_hold_cover",
                "estimated_fee_bps": 12.5,
                "estimated_borrow_bps": 8.0,
            }
        )

        self.assertTrue(result["applies"])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")
        self.assertEqual(result["failed_checks"], [])


class PaperOnlyFrontierShortRouteProfileGateTests(unittest.TestCase):
    def test_preserves_legacy_non_applicable_short_profile_behavior(self):
        result = paper_only_frontier_short_route_profile_gate(
            {
                "fresh_profile": True,
                "simulated_fill_quality": 0.92,
                "simulated_spread_bps": 10.0,
                "simulated_borrow_state": "available",
                "asset_class": "equity",
                "market_type": "spot",
                "side": "short",
                "trigger_type": "conditional",
            }
        )

        self.assertTrue(result["eligible"])
        self.assertFalse(result["feasibility_gate_applied"])
        self.assertEqual(result["reason"], "eligible")

    def test_rejects_conditional_crypto_spot_short_when_route_is_structurally_infeasible(self):
        result = paper_only_frontier_short_route_profile_gate(
            {
                "fresh_profile": True,
                "simulated_fill_quality": 0.95,
                "simulated_spread_bps": 9.0,
                "simulated_borrow_state": "available",
                "asset_class": "crypto",
                "market_type": "spot",
                "side": "short",
                "trigger_type": "conditional",
                "venue_shorting_supported": True,
                "instrument_margin_shortable": True,
                "route_lifecycle": ["open", "hold"],
                "estimated_fee_bps": 7.5,
            }
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(result["feasibility_gate_applied"])
        self.assertEqual(result["reason"], "paper_short_route_infeasible")
        self.assertIn("borrow_assumptions_present", result["conditional_spot_short_feasibility"]["failed_checks"])
