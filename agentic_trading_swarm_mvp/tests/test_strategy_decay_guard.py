import unittest

from src.frontier_crypto_adapter import paper_only_strategy_decay_guard


class StrategyDecayGuardTests(unittest.TestCase):
    def test_blocks_two_sided_negative_expectancy(self):
        result = paper_only_strategy_decay_guard(
            strategy_family="YAHOO proxy momentum",
            long_expectancy_recent=-0.12,
            short_expectancy_recent=-0.08,
            sample_size_recent=25,
            min_sample_size=20,
            negative_margin=0.01,
        )

        self.assertEqual(result["strategy_decay_state"], "blocked_for_paper_selection")
        self.assertFalse(result["recovery_gate"])
        self.assertGreater(result["decay_score"], 0.0)

    def test_allows_recovery_when_one_side_improves(self):
        result = paper_only_strategy_decay_guard(
            strategy_family="YAHOO proxy momentum",
            long_expectancy_recent=0.03,
            short_expectancy_recent=-0.04,
            sample_size_recent=25,
            min_sample_size=20,
            negative_margin=0.01,
            recovery_expectancy=0.0,
        )

        self.assertEqual(result["strategy_decay_state"], "active")
        self.assertTrue(result["recovery_gate"])

    def test_unknown_when_inputs_missing(self):
        result = paper_only_strategy_decay_guard(strategy_family="proxy momentum")

        self.assertEqual(result["strategy_decay_state"], "unknown")
        self.assertFalse(result["recovery_gate"])


if __name__ == "__main__":
    unittest.main()
