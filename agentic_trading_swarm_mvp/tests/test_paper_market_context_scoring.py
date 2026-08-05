import unittest

from src.frontier_crypto_adapter import frontier_short_market_context_review
from src.proxy_signal_quality import proxy_momentum_context_review


class FrontierShortMarketContextTests(unittest.TestCase):
    def _observation(self) -> dict:
        return {
            "venue": "LOCAL",
            "comparison_key": "ABC|USDT",
            "market_type": "spot",
            "data_status": "reachable",
            "quote": "USDT",
            "last": 101.0,
        }

    def _candidate(self, **overrides: object) -> dict:
        candidate = {
            "direction": "short_frontier_spot",
            "venue_deviation_bps": 40.0,
            "spread_bps": 2.0,
            "entry_slippage_bps_estimate": 2.0,
            "exit_slippage_bps_estimate": 2.0,
            "local_short_horizon_trend_bps": -3.0,
        }
        candidate.update(overrides)
        return candidate

    def _peer(self, venue: str, change_24h_pct: float) -> dict:
        return {
            "venue": venue,
            "comparison_key": "ABC|USDT",
            "market_type": "spot",
            "data_status": "reachable",
            "quote": "USDT",
            "last": 100.0,
            "change_24h_pct": change_24h_pct,
        }

    def test_confirmed_context_reports_all_frontier_short_dimensions(self) -> None:
        review = frontier_short_market_context_review(
            self._observation(),
            self._candidate(),
            [self._peer("REF1", -0.8), self._peer("REF2", -0.5), self._peer("REF3", -0.2)],
            {"mode": "paper", "allow_live_trading": False},
        )

        self.assertTrue(review["applicable"])
        self.assertTrue(review["confirmed"])
        self.assertEqual([], review["diagnostics"])
        self.assertEqual("primary_simulated_route", review["emission_action"])
        self.assertEqual(1.0, review["allocation_multiplier"])
        self.assertEqual(3, review["reference_breadth"])
        self.assertEqual(1.0, review["broader_risk_off_ratio"])

    def test_weak_context_is_counterfactual_but_not_a_paper_entry_block(self) -> None:
        review = frontier_short_market_context_review(
            self._observation(),
            self._candidate(
                venue_deviation_bps=8.0,
                spread_bps=4.0,
                entry_slippage_bps_estimate=3.0,
                exit_slippage_bps_estimate=3.0,
                local_short_horizon_trend_bps=2.0,
            ),
            [self._peer("REF1", 0.4)],
            {"mode": "paper", "allow_live_trading": False},
        )

        self.assertFalse(review["confirmed"])
        self.assertEqual("counterfactual_guard_value", review["emission_action"])
        self.assertGreaterEqual(review["allocation_multiplier"], 0.25)
        self.assertIn("reference_breadth_below_context_target", review["diagnostics"])
        self.assertIn("broader_risk_off_not_confirmed", review["diagnostics"])
        self.assertNotIn("paper_entry_blocked", review)


class ProxyMomentumContextTests(unittest.TestCase):
    def _candidate(self, **overrides: object) -> dict:
        candidate = {
            "venue": "YAHOO_PROXY",
            "trade_type": "global_proxy_momentum",
            "direction": "short_proxy",
            "change_24h_pct": -1.0,
            "short_return_pct": -0.2,
            "provider_age_seconds": 20.0,
            "recent_volatility_bps": 10.0,
        }
        candidate.update(overrides)
        return candidate

    def test_strong_fresh_followthrough_is_confirmed(self) -> None:
        review = proxy_momentum_context_review(self._candidate())

        self.assertTrue(review["applicable"])
        self.assertTrue(review["confirmed"])
        self.assertEqual("primary_simulated_route", review["emission_action"])
        self.assertEqual(1.0, review["allocation_multiplier"])
        self.assertEqual([], review["diagnostics"])

    def test_weak_or_stale_proxy_stays_observable_as_counterfactual(self) -> None:
        review = proxy_momentum_context_review(
            self._candidate(
                change_24h_pct=-0.1,
                short_return_pct=0.1,
                provider_age_seconds=1200.0,
                recent_volatility_bps=100.0,
            )
        )

        self.assertFalse(review["confirmed"])
        self.assertEqual("counterfactual_guard_value", review["emission_action"])
        self.assertGreaterEqual(review["allocation_multiplier"], 0.25)
        self.assertIn("proxy_freshness_degraded", review["diagnostics"])
        self.assertIn("tradable_followthrough_not_confirmed", review["diagnostics"])

    def test_live_mode_is_out_of_scope(self) -> None:
        review = proxy_momentum_context_review(self._candidate(execution_mode="live"))

        self.assertFalse(review["applicable"])
        self.assertEqual("unchanged", review["emission_action"])


if __name__ == "__main__":
    unittest.main()
