import unittest

from src.frontier_crypto_adapter import paper_only_premarket_liquidity_gate


class PremarketLiquidityGateTests(unittest.TestCase):
    def test_rejects_low_dollar_volume(self):
        self.assertFalse(
            paper_only_premarket_liquidity_gate(
                dollar_volume_usd=1499999,
                spread_pct=0.1,
                recent_trade_count=50,
                recent_print_window_minutes=10,
            )
        )

    def test_rejects_wide_spread(self):
        self.assertFalse(
            paper_only_premarket_liquidity_gate(
                dollar_volume_usd=2000000,
                spread_pct=0.76,
                recent_trade_count=50,
                recent_print_window_minutes=10,
            )
        )

    def test_rejects_sparse_recent_prints(self):
        self.assertFalse(
            paper_only_premarket_liquidity_gate(
                dollar_volume_usd=2000000,
                spread_pct=0.1,
                recent_trade_count=50,
                recent_print_window_minutes=4,
            )
        )

    def test_accepts_liquid_candidate(self):
        self.assertTrue(
            paper_only_premarket_liquidity_gate(
                dollar_volume_usd=2500000,
                spread_pct=0.5,
                recent_trade_count=25,
                recent_print_window_minutes=6,
            )
        )


if __name__ == "__main__":
    unittest.main()
