import unittest

from src.frontier_crypto_adapter import paper_only_volatility_liquidity_entry_gate


class PaperOnlyVolatilityLiquidityEntryGateTests(unittest.TestCase):
    def test_all_thresholds_pass(self):
        result = paper_only_volatility_liquidity_entry_gate(
            spread_bps=10.0,
            realized_volatility_zscore=1.5,
            recent_volume_ratio=1.2,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")

    def test_spread_rejects_entry(self):
        result = paper_only_volatility_liquidity_entry_gate(
            spread_bps=15.0,
            realized_volatility_zscore=1.5,
            recent_volume_ratio=1.2,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "spread_above_threshold")

    def test_volatility_rejects_entry(self):
        result = paper_only_volatility_liquidity_entry_gate(
            spread_bps=10.0,
            realized_volatility_zscore=2.5,
            recent_volume_ratio=1.2,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "volatility_above_threshold")

    def test_volume_rejects_entry(self):
        result = paper_only_volatility_liquidity_entry_gate(
            spread_bps=10.0,
            realized_volatility_zscore=1.5,
            recent_volume_ratio=1.0,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "volume_below_baseline")


if __name__ == "__main__":
    unittest.main()
