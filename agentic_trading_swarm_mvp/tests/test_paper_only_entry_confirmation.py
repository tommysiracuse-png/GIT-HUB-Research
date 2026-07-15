import unittest

from src.frontier_crypto_adapter import paper_only_long_entry_confirmation


class PaperOnlyEntryConfirmationTests(unittest.TestCase):
    def test_all_conditions_allow_entry(self):
        gate = paper_only_long_entry_confirmation(
            price=101.0,
            ema_20=100.0,
            rsi_1h=56.0,
            volume=120.0,
            avg_volume_20=100.0,
        )
        self.assertTrue(gate["allowed"])
        self.assertTrue(gate["price_above_ema20"])
        self.assertTrue(gate["rsi_ok"])
        self.assertTrue(gate["volume_ok"])
        self.assertEqual(gate["reasons"], [])

    def test_blocks_when_volume_is_too_low(self):
        gate = paper_only_long_entry_confirmation(
            price=101.0,
            ema_20=100.0,
            rsi_1h=56.0,
            volume=119.0,
            avg_volume_20=100.0,
        )
        self.assertFalse(gate["allowed"])
        self.assertIn("volume_below_min_ratio", gate["reasons"])

    def test_blocks_when_price_and_rsi_fail(self):
        gate = paper_only_long_entry_confirmation(
            price=99.0,
            ema_20=100.0,
            rsi_1h=55.0,
            volume=150.0,
            avg_volume_20=100.0,
        )
        self.assertFalse(gate["allowed"])
        self.assertIn("price_below_ema20", gate["reasons"])
        self.assertIn("rsi_below_min", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
