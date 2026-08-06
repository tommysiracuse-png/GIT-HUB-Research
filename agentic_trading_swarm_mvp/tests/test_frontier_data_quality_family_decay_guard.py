import unittest

from src.frontier_data_quality import _paper_only_family_decay_guard_review


class PaperOnlyFamilyDecayGuardTests(unittest.TestCase):
    def test_blocks_target_proxy_family_with_bilateral_negative_paper_edge(self):
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
        self.assertTrue(review["bilateral_failure"])
        self.assertEqual(["long", "short"], review["failed_legs"])
        self.assertLess(review["rolling_expectancy_bps"], 0.0)

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

    def test_matches_full_signal_key_and_reopens_when_one_leg_recovers(self):
        review = _paper_only_family_decay_guard_review(
            {
                "signal_key": "YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional",
                "direction": "short",
                "execution_mode": "paper",
                "latest_family_paper": {
                    "long_proxy_standard": {"closed_count": 30, "avg_pnl_bps": 1.25, "win_rate": 0.52},
                    "short_proxy_conditional": {"closed_count": 28, "avg_pnl_bps": -3.0, "win_rate": 0.41},
                },
            }
        )

        self.assertTrue(review["applies"])
        self.assertFalse(review["blocked"])
        self.assertEqual("family_decay_recovered", review["reason"])
        self.assertFalse(review["bilateral_failure"])
        self.assertEqual("YAHOO_PROXY", review["market_key"])
        self.assertTrue(review["recovery_status"]["current_recovered"])

    def test_source_signal_key_descendant_inherits_family_decay_guard(self):
        review = _paper_only_family_decay_guard_review(
            {
                "market_key": "OKX_PERP|frontier_crypto_venue_map|long_frontier_perp|standard",
                "source_signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                "signal_family": "global_proxy_momentum",
                "direction": "long_frontier_perp",
                "execution_mode": "paper",
            }
        )

        self.assertTrue(review["applies"])
        self.assertTrue(review["blocked"])
        self.assertEqual("YAHOO_PROXY", review["market_key"])
        self.assertEqual("family_decay_suppressed", review["reason"])
        self.assertEqual(["long", "short"], review["failed_legs"])

    def test_recovery_evidence_can_release_static_decay_snapshot(self):
        passing_window = {
            "sample_count": 12,
            "after_cost_expectancy_bps": 0.1,
            "freshness_pass_rate": 0.95,
            "execution_quality_pass_rate": 0.96,
        }
        review = _paper_only_family_decay_guard_review(
            {
                "signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                "execution_mode": "paper",
            },
            {
                "strategy_lab": {
                    "yahoo_proxy_momentum_source_veto": {
                        "recovery_evidence": {
                            "source_family": {"windows": [passing_window] * 3},
                            "immediate_descendants": {"windows": [passing_window] * 3},
                        }
                    }
                }
            },
        )

        self.assertTrue(review["applies"])
        self.assertFalse(review["blocked"])
        self.assertEqual("family_decay_recovered", review["reason"])
        self.assertTrue(review["recovery_status"]["recovered_by_windows"])
        self.assertTrue(review["recovery_status"]["scopes"]["source_family"]["recovered"])
        self.assertTrue(review["recovery_status"]["scopes"]["immediate_descendants"]["recovered"])
