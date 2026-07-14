import unittest

from src.frontier_crypto_adapter import DEFAULT_PAPER_TRADE_POLICY, DEFAULT_REGISTRY


class FrontierCryptoAdapterPolicyTests(unittest.TestCase):
    def test_default_paper_trade_policy_is_paper_only_and_conservative(self) -> None:
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["mode"], "paper_only")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["execution"], "simulated")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["min_confirmation_score"], 0.70)
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["divergence_block"], "enabled")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["high_volatility_posture"], "monitor_first")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["single_asset_override"], "disabled")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["state_if_unconfirmed"], "flat")
        self.assertEqual(DEFAULT_PAPER_TRADE_POLICY["state_if_divergent"], "monitor")

    def test_default_registry_enables_paper_trade_policy(self) -> None:
        filters = DEFAULT_REGISTRY["filters"]
        self.assertTrue(filters["paper_trade_policy_enabled"])
        self.assertGreaterEqual(filters["min_cross_venue_count"], 2)
        self.assertEqual(DEFAULT_REGISTRY["paper_trade_policy"]["mode"], "paper_only")


if __name__ == "__main__":
    unittest.main()
