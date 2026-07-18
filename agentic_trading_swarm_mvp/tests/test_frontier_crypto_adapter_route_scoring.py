import unittest

from src.frontier_crypto_adapter import paper_only_adjusted_route_score


class PaperOnlyAdjustedRouteScoreTests(unittest.TestCase):
    def test_adjusted_score_penalizes_stale_thin_routes(self):
        packet = paper_only_adjusted_route_score(
            raw_edge_bps=10,
            estimated_slippage_bps=4,
            quote_age_ms=1200,
            top_of_book_notional_usd=10000,
        )
        self.assertFalse(packet["passes_route_guard"])
        self.assertLess(packet["adjusted_edge_bps"], 10)
        self.assertGreater(packet["stale_quote_penalty_bps"], 0)
        self.assertGreater(packet["thin_depth_penalty_bps"], 0)

    def test_adjusted_score_allows_fresh_deep_routes(self):
        packet = paper_only_adjusted_route_score(
            raw_edge_bps=10,
            estimated_slippage_bps=4,
            quote_age_ms=200,
            top_of_book_notional_usd=50000,
        )
        self.assertTrue(packet["passes_route_guard"])
        self.assertEqual(packet["stale_quote_penalty_bps"], 0.0)
        self.assertEqual(packet["thin_depth_penalty_bps"], 0.0)
        self.assertEqual(packet["adjusted_edge_bps"], 6.0)


if __name__ == "__main__":
    unittest.main()
