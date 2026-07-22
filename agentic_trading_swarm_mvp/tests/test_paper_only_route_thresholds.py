import unittest

from src.frontier_crypto_adapter import _PAPER_ONLY_PREMARKET_LIQUIDITY_DEFAULTS


class PaperOnlyRouteThresholdsTests(unittest.TestCase):
    def test_premarket_liquidity_defaults_are_tightened(self):
        self.assertGreaterEqual(
            _PAPER_ONLY_PREMARKET_LIQUIDITY_DEFAULTS["min_premarket_dollar_volume_usd"],
            2500000.0,
        )
        self.assertLessEqual(_PAPER_ONLY_PREMARKET_LIQUIDITY_DEFAULTS["max_spread_pct"], 0.5)


if __name__ == "__main__":
    unittest.main()
