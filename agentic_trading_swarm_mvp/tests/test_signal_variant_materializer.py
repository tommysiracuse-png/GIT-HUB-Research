import unittest

from src.signals.frontier_crypto_venue_map import choose_favorable_frontier_signal


class SignalVariantMaterializerTests(unittest.TestCase):
    def test_favorable_signal_can_be_materialized_from_venue_map(self):
        chosen = choose_favorable_frontier_signal(
            {"spot_a": 100, "spot_b": 101, "spot_c": 106},
            min_peer_count=2,
        )
        self.assertIsNotNone(chosen)
        self.assertEqual("spot_c", chosen.venue)
        self.assertGreater(chosen.delta_vs_reference, 0)

    def test_sparse_peer_coverage_returns_none(self):
        self.assertIsNone(
            choose_favorable_frontier_signal({"spot_a": 100, "spot_b": 101}, min_peer_count=2)
        )
