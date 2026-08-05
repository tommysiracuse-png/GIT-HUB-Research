import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from contextual_failure_filters import (
    annotate_candidates_with_cross_context_diagnostics,
    cross_context_failure_observations,
)
from llm_bridge import _compact_contextual_failures


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

    def test_persistent_context_is_annotated_and_sized_without_paper_entry_block(self):
        observations = [
            {
                "context": "okx_basis_or_funding",
                "state": "persistent_failure",
                "closed_count": 6,
                "avg_pnl_bps": -18.0,
                "win_rate": 0.17,
                "research_note": "Carry friction dominates quoted edge.",
                "rehabilitation_criteria": {"validation_window_closed_trades": 3},
            }
        ]
        candidate = {
            "venue": "OKX",
            "direction": "funding_capture_short_perp",
            "trade_type": "perp_funding_basis",
            "signal_key": "OKX|perp_funding_basis|funding_capture_short_perp|standard",
            "score": 100.0,
            "paper_allocation_multiplier": 1.0,
        }

        annotated = annotate_candidates_with_cross_context_diagnostics(
            [candidate],
            observations,
            {
                "contextual_failure_filters": {
                    "cross_context_failure_allocation_multiplier": 0.2,
                    "cross_context_failure_score_multiplier": 0.8,
                }
            },
        )[0]

        self.assertEqual(annotated["score"], 80.0)
        self.assertEqual(annotated["paper_allocation_multiplier"], 0.2)
        self.assertFalse(annotated["cross_context_failure_diagnostic"]["paper_entry_blocked"])
        self.assertEqual(
            annotated["cross_context_failure_diagnostic"]["recommendation_handling"],
            "diagnostic_ranking_and_sizing_only",
        )
        self.assertNotIn("paper_entry_blocked", annotated)

    def test_compact_research_packet_preserves_cross_context_rehabilitation_details(self):
        compact = _compact_contextual_failures(
            {
                "cross_context_observations": [
                    {
                        "context": "yahoo_proxy_momentum",
                        "state": "rehabilitated",
                        "closed_count": 8,
                        "avg_pnl_bps": 12.0,
                        "win_rate": 0.625,
                        "directions": ["long", "short"],
                        "research_note": "Fresh validation has improved.",
                        "rehabilitation_criteria": {"validation_window_closed_trades": 3},
                    }
                ]
            }
        )

        observation = compact["cross_context_observations"][0]
        self.assertEqual(observation["context"], "yahoo_proxy_momentum")
        self.assertEqual(observation["state"], "rehabilitated")
        self.assertFalse(observation["paper_entry_blocked"])


if __name__ == "__main__":
    unittest.main()
