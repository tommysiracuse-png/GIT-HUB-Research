import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from contextual_failure_filters import cross_context_failure_observations


class CrossContextFailureObservationTests(unittest.TestCase):
    @staticmethod
    def _trade(signal_key, venue, direction, pnl_bps):
        return {
            "signal_key": signal_key,
            "pnl_bps": pnl_bps,
            "candidate": {
                "venue": venue,
                "direction": direction,
                "trade_type": "frontier_crypto_venue_map",
            },
            "features": {"venue": venue, "direction": direction, "trade_type": "frontier_crypto_venue_map"},
        }

    def test_frontier_multi_venue_losses_are_attributed_without_blocking_paper(self):
        trades = [
            self._trade("VALR|frontier_crypto_venue_map|long_frontier_spot", "VALR", "long_frontier_spot", -30),
            self._trade("BITSO|frontier_crypto_venue_map|short_frontier_spot", "BITSO", "short_frontier_spot", -20),
            self._trade("VALR|frontier_crypto_venue_map|long_frontier_spot", "VALR", "long_frontier_spot", -10),
            self._trade("BITSO|frontier_crypto_venue_map|short_frontier_spot", "BITSO", "short_frontier_spot", -40),
        ]

        observations = cross_context_failure_observations(
            trades,
            {"contextual_failure_filters": {"cross_context_min_closed": 4, "cross_context_validation_window": 3}},
        )

        observation = observations[0]
        self.assertEqual(observation["context"], "frontier_spot_venue_map")
        self.assertEqual(observation["state"], "persistent_failure")
        self.assertTrue(observation["coverage"]["both_directions"])
        self.assertTrue(observation["coverage"]["multi_venue"])
        self.assertFalse(observation["paper_entry_blocked"])
        self.assertEqual(observation["recommendation_handling"], "diagnostic_ranking_and_sizing_only")

    def test_fresh_profitable_validation_rehabilitates_an_observed_context(self):
        trades = [
            self._trade("VALR|frontier_crypto_venue_map|long_frontier_spot", "VALR", "long_frontier_spot", -35),
            self._trade("BITSO|frontier_crypto_venue_map|short_frontier_spot", "BITSO", "short_frontier_spot", -25),
            self._trade("VALR|frontier_crypto_venue_map|long_frontier_spot", "VALR", "long_frontier_spot", 30),
            self._trade("BITSO|frontier_crypto_venue_map|short_frontier_spot", "BITSO", "short_frontier_spot", 20),
        ]

        observations = cross_context_failure_observations(
            trades,
            {
                "contextual_failure_filters": {
                    "cross_context_min_closed": 4,
                    "cross_context_validation_window": 2,
                    "release_min_avg_pnl_bps": 10,
                    "release_min_win_rate": 0.55,
                }
            },
        )

        self.assertEqual(observations[0]["state"], "rehabilitated")
        self.assertFalse(observations[0]["paper_entry_blocked"])


if __name__ == "__main__":
    unittest.main()
