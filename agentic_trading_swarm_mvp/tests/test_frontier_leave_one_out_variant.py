import unittest

from src.signals.frontier_crypto_venue_map import (
    build_leave_one_out_frontier_signals,
    choose_favorable_frontier_signal,
)


class FrontierLeaveOneOutVariantTests(unittest.TestCase):
    def test_builds_leave_one_out_reference_per_venue(self):
        signals = build_leave_one_out_frontier_signals(
            {"A": 10, "B": 12, "C": 14},
            min_peer_count=2,
        )
        self.assertEqual(3, len(signals))
        by_venue = {signal.venue: signal for signal in signals}
        self.assertAlmostEqual(12.0, by_venue["B"].reference_median)
        self.assertAlmostEqual(0.0, by_venue["B"].delta_vs_reference)

    def test_chooses_only_favorable_frontier_signal(self):
        chosen = choose_favorable_frontier_signal(
            {"A": 10, "B": 10, "C": 15, "D": 11},
            min_peer_count=2,
        )
        self.assertIsNotNone(chosen)
        self.assertEqual("C", chosen.venue)
        self.assertTrue(chosen.is_favorable)

    def test_requires_sufficient_peer_coverage(self):
        signals = build_leave_one_out_frontier_signals({"A": 10, "B": 11}, min_peer_count=2)
        self.assertEqual([], signals)
