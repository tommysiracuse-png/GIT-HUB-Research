import unittest

from src.frontier_data_quality import _paper_only_family_decay_guard_review


class PaperOnlyFamilyDecayGuardTests(unittest.TestCase):
    def test_blocks_target_proxy_family_with_zero_score_multiplier(self):
        review = _paper_only_family_decay_guard_review(
            {
                "market_key": "YAHOO_PROXY",
                "strategy_family": "global_proxy_momentum",
                "direction": "long",
                "raw_score": 5.75,
                "freshness_state": "fresh",
                "execution_mode": "paper",
            }
        )

        self.assertTrue(review["enabled"])
        self.assertTrue(review["applies"])
        self.assertTrue(review["blocked"])
        self.assertFalse(review["eligible"])
        self.assertEqual(review["reason"], "family_decay_suppressed")
        self.assertEqual(review["event"], "family_decay_suppressed")
        self.assertEqual(review["family"], "YAHOO_PROXY|global_proxy_momentum")
        self.assertEqual(review["attempted_direction"], "long")
        self.assertEqual(review["raw_score"], 5.75)
        self.assertEqual(review["freshness_state"], "fresh")
        self.assertEqual(review["paper_score_multiplier"], 0.0)

        latest_family_paper = review["latest_family_paper"]
        self.assertEqual(latest_family_paper["long_proxy_standard"]["closed_count"], 176)
        self.assertAlmostEqual(latest_family_paper["long_proxy_standard"]["avg_pnl_bps"], -16.225)
        self.assertEqual(latest_family_paper["short_proxy_conditional"]["closed_count"], 171)
        self.assertAlmostEqual(latest_family_paper["short_proxy_conditional"]["win_rate"], 0.322)

    def test_disables_guard_outside_paper_mode(self):
        review = _paper_only_family_decay_guard_review(
            {
                "market_key": "YAHOO_PROXY",
                "strategy_family": "global_proxy_momentum",
                "direction": "short",
                "raw_score": 3.5,
                "freshness_state": "stale",
                "execution_mode": "live",
            }
        )

        self.assertFalse(review["enabled"])
        self.assertTrue(review["applies"])
        self.assertFalse(review["blocked"])
        self.assertTrue(review["eligible"])
        self.assertEqual(review["reason"], "non_paper_mode")
        self.assertIsNone(review["event"])
        self.assertEqual(review["paper_score_multiplier"], 1.0)

    def test_ignores_other_families(self):
        review = _paper_only_family_decay_guard_review(
            {
                "market_key": "YAHOO_PROXY",
                "strategy_family": "proxy_mean_reversion",
                "direction": "long",
                "raw_score": 2.0,
            }
        )

        self.assertTrue(review["enabled"])
        self.assertFalse(review["applies"])
        self.assertFalse(review["blocked"])
        self.assertEqual(review["paper_score_multiplier"], 1.0)
