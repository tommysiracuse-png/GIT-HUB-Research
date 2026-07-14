import unittest

from src.frontier_crypto_adapter import paper_only_executable_quality_check


class PaperOnlyExecutableQualityCheckTests(unittest.TestCase):
    def test_passes_when_edge_spread_depth_and_freshness_are_acceptable(self):
        result = paper_only_executable_quality_check(
            expected_edge_bps=28.0,
            quoted_spread_bps=6.0,
            top_of_book_depth=9.0,
            paper_order_size=2.0,
            quote_age_ms=900.0,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["score_multiplier"], 1.0)

    def test_rejects_stale_quotes_and_insufficient_net_edge(self):
        result = paper_only_executable_quality_check(
            expected_edge_bps=15.0,
            quoted_spread_bps=4.0,
            top_of_book_depth=10.0,
            paper_order_size=2.0,
            quote_age_ms=2500.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("quote_stale", result["reasons"])
        self.assertIn("net_edge_below_minimum", result["reasons"])
        self.assertEqual(result["score_multiplier"], 0.0)

    def test_rejects_spread_and_depth_failures(self):
        result = paper_only_executable_quality_check(
            expected_edge_bps=40.0,
            quoted_spread_bps=20.0,
            top_of_book_depth=4.0,
            paper_order_size=2.0,
            quote_age_ms=100.0,
            min_depth_multiple_of_paper_size=3.0,
        )
        self.assertFalse(result["passed"])
        self.assertIn("spread_exceeds_edge_fraction", result["reasons"])
        self.assertIn("insufficient_depth", result["reasons"])


if __name__ == "__main__":
    unittest.main()
