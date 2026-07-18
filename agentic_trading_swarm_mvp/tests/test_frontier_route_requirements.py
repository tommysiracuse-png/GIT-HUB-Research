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

    def test_unsupported_conditional_short_is_suppressed(self):
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
        self.assertEqual(gate["reason"], "unsupported_spot_short")

    def test_score_adjustment_reports_unknown_route_penalty_without_full_suppression(self):
        adjustment = paper_only_frontier_score_adjustment(
            venue="BITSO",
            direction="short",
            context_stats={
                "closed_trade_count": 10,
                "recent_expectancy_bps": 6.0,
                "conditional": True,
                "context_key": "feasibility:conditional",
                "cross_market_divergence_bps": 2.0,
                "cross_market_trigger_bps": 1.0,
                "source_a_freshness_ms": 25.0,
                "source_b_freshness_ms": 25.0,
            },
            registry={
                "BITSO_SHORT": {
                    "enabled": True,
                    "min_closed_trades": 1,
                    "min_expectancy_bps": -999.0,
                }
            },
            route_feasibility_policy={
                "unknown_multiplier": 0.8,
            },
        )
        self.assertTrue(adjustment["allow"])
        self.assertFalse(adjustment["suppressed"])
        self.assertAlmostEqual(adjustment["score_multiplier"], 0.8)
        self.assertEqual(
            adjustment["route_feasibility_gate"]["route_requirements"]["support_status"],
            "unknown",
        )
        self.assertEqual(
            adjustment["route_feasibility_gate"]["reason"],
            "unknown_spot_short_support",
        )

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


if __name__ == "__main__":
    unittest.main()
