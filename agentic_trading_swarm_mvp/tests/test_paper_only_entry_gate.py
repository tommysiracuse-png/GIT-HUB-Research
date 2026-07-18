import unittest

from src.frontier_crypto_adapter import paper_only_liquidity_volatility_entry_gate


class PaperOnlyLiquidityVolatilityEntryGateTests(unittest.TestCase):
    def test_eligible_when_liquidity_and_spread_meet_tighter_paper_only_defaults(self):
        result = paper_only_liquidity_volatility_entry_gate(
            spread_bps=34.5,
            realized_volatility_zscore=1.8,
            volume_ratio=1.25,
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")
        self.assertEqual(result["max_spread_bps"], 35.0)
        self.assertEqual(result["min_volume_ratio"], 1.25)

    def test_rejects_when_volume_is_below_paper_only_threshold(self):
        result = paper_only_liquidity_volatility_entry_gate(
            spread_bps=12.0,
            realized_volatility_zscore=1.0,
            volume_ratio=1.24,
        )

        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "volume_below_threshold")


if __name__ == "__main__":
    unittest.main()
