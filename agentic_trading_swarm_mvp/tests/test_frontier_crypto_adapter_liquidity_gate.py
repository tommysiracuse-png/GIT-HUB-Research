import unittest

from src.frontier_crypto_adapter import _paper_only_route_liquidity_gate


class FrontierCryptoAdapterLiquidityGateTests(unittest.TestCase):
    def test_gate_passes_for_tight_spread_and_sufficient_depth(self):
        packet = _paper_only_route_liquidity_gate(
            spread_bps=8,
            top_of_book_notional_usd=50000,
            max_spread_bps=12,
            min_top_of_book_notional_usd=25000,
        )

        self.assertTrue(packet["paper_only"])
        self.assertTrue(packet["passed"])
        self.assertEqual(packet["reasons"], [])

    def test_gate_rejects_wide_spread_or_shallow_book(self):
        packet = _paper_only_route_liquidity_gate(
            spread_bps=18,
            top_of_book_notional_usd=12000,
            max_spread_bps=12,
            min_top_of_book_notional_usd=25000,
        )

        self.assertFalse(packet["passed"])
        self.assertIn("wide_spread", packet["reasons"])
        self.assertIn("shallow_book", packet["reasons"])


if __name__ == "__main__":
    unittest.main()
