import unittest

from src.frontier_crypto_adapter import (
    paper_only_cross_market_risk_gate,
    paper_only_frontier_score_adjustment,
)


class CrossMarketPaperGateTests(unittest.TestCase):
    def test_missing_cross_market_inputs_are_neutral(self):
        gate = paper_only_cross_market_risk_gate()

        self.assertFalse(gate["applicable"])
        self.assertFalse(gate["allow_record"])
        self.assertFalse(gate["close_position"])
        self.assertEqual(gate["score_multiplier"], 1.0)
        self.assertEqual(gate["reason"], "insufficient_inputs")

    def test_allows_record_when_divergence_exceeds_trigger_and_sources_are_fresh(self):
        gate = paper_only_cross_market_risk_gate(
            divergence_bps=3.0,
            trigger_bps=2.0,
            source_a_freshness_ms=100.0,
            source_b_freshness_ms=120.0,
            freshness_limit_ms=500.0,
        )
        self.assertTrue(gate["allow_record"])
        self.assertFalse(gate["close_position"])
        self.assertEqual(gate["score_multiplier"], 1.0)
        self.assertTrue(gate["applicable"])

    def test_closes_when_mean_reverted_or_stale(self):
        gate = paper_only_cross_market_risk_gate(
            divergence_bps=0.2,
            trigger_bps=2.0,
            source_a_freshness_ms=100.0,
            source_b_freshness_ms=1200.0,
            freshness_limit_ms=500.0,
        )
        self.assertFalse(gate["allow_record"])
        self.assertTrue(gate["close_position"])
        self.assertEqual(gate["score_multiplier"], 0.0)
        self.assertTrue(gate["applicable"])

    def test_adjustment_includes_cross_market_gate_multiplier(self):
        adjustment = paper_only_frontier_score_adjustment(
            venue="OKX_SPOT",
            direction="LONG",
            context_stats={
                "cross_market_divergence_bps": 3.0,
                "cross_market_trigger_bps": 2.0,
                "source_a_freshness_ms": 100.0,
                "source_b_freshness_ms": 120.0,
                "freshness_limit_ms": 500.0,
            },
            registry={},
            enabled=True,
        )
        self.assertIn("cross_market_gate", adjustment)
        self.assertEqual(adjustment["cross_market_gate"]["allow_record"], True)

    def test_adjustment_stays_neutral_without_cross_market_inputs(self):
        adjustment = paper_only_frontier_score_adjustment(
            venue="OKX_SPOT",
            direction="LONG",
            context_stats={
                "closed_trade_count": 10,
                "recent_expectancy_bps": 2.0,
                "recent_win_rate": 0.60,
                "low_feasibility_share": 0.10,
            },
            enabled=True,
        )

        self.assertIn("cross_market_gate", adjustment)
        self.assertFalse(adjustment["cross_market_gate"]["applicable"])
        self.assertTrue(adjustment["allow"])
        self.assertFalse(adjustment["suppressed"])
        self.assertGreaterEqual(adjustment["score_multiplier"], 1.0)


if __name__ == "__main__":
    unittest.main()
