import unittest

from src.frontier_crypto_adapter import (
    paper_only_frontier_long_cohort_gate,
    paper_only_frontier_score_adjustment,
    paper_only_frontier_venue_direction_expectancy_gate,
)


class FrontierCryptoAdapterPaperGatesTest(unittest.TestCase):
    def test_venue_direction_gate_allows_allowlisted_positive_case(self):
        verdict = paper_only_frontier_venue_direction_expectancy_gate(
            venue="OKX_SPOT",
            direction="LONG",
            context_stats={
                "closed_trade_count": 10,
                "recent_expectancy_bps": 1.0,
                "confidence": 0.7,
                "score_multiplier": 1.0,
            },
        )
        self.assertTrue(verdict["allow"])
        self.assertEqual(verdict["reason"], "allowlisted")

    def test_long_cohort_gate_suppresses_only_on_weak_recent_stats(self):
        verdict = paper_only_frontier_long_cohort_gate(
            closed_trade_count=20,
            recent_expectancy_bps=-10.0,
            recent_win_rate=0.30,
            low_feasibility_share=0.50,
        )
        self.assertTrue(verdict["suppressed"])
        self.assertLess(verdict["score_multiplier"], 1.0)

    def test_score_adjustment_combines_both_gates(self):
        verdict = paper_only_frontier_score_adjustment(
            venue="OKX_SPOT",
            direction="LONG",
            context_stats={
                "closed_trade_count": 10,
                "recent_expectancy_bps": 2.0,
                "recent_win_rate": 0.60,
                "low_feasibility_share": 0.10,
                "confidence": 0.80,
                "score_multiplier": 1.0,
            },
        )
        self.assertTrue(verdict["allow"])
        self.assertFalse(verdict["suppressed"])
        self.assertGreaterEqual(verdict["score_multiplier"], 1.0)


if __name__ == "__main__":
    unittest.main()
