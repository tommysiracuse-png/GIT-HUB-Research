import json
from pathlib import Path
import unittest


class FrontierCryptoVenuesExampleConfigTest(unittest.TestCase):
    def test_breakout_quality_filter_contains_paper_only_controls(self):
        config_path = Path("config/frontier_crypto_venues.example.json")
        payload = json.loads(config_path.read_text(encoding="utf-8"))

        filter_cfg = payload["paper_only_rules"]["breakout_quality_filter"]

        self.assertEqual(filter_cfg["mode"], "paper_only")
        self.assertEqual(filter_cfg["scanner_profile"], "breakout_momentum_v2")
        self.assertEqual(filter_cfg["base_breakout_buffer_pct"], 0.2)
        self.assertEqual(filter_cfg["high_atr_pct_threshold"], 4.5)
        self.assertEqual(filter_cfg["high_vol_breakout_buffer_pct"], 0.35)
        self.assertEqual(filter_cfg["min_rel_volume"], 1.8)
        self.assertEqual(filter_cfg["max_spread_pct"], 0.35)
        self.assertTrue(filter_cfg["require_above_vwap"])
        self.assertTrue(filter_cfg["require_above_20ema"])


if __name__ == "__main__":
    unittest.main()
import json
from pathlib import Path
import unittest


class TestFrontierCryptoVenuesExampleConfig(unittest.TestCase):
    def test_breakout_quality_filter_paper_only_defaults(self):
        config_path = Path("config/frontier_crypto_venues.example.json")
        data = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertIn("paper_only_rules", data)
        paper_rules = data["paper_only_rules"]
        self.assertTrue(paper_rules["require_complete_json"])

        breakout = paper_rules["breakout_quality_filter"]
        self.assertEqual(breakout["mode"], "paper_only")
        self.assertTrue(breakout["require_above_vwap"])
        self.assertTrue(breakout["require_above_20ema"])
        self.assertAlmostEqual(breakout["base_breakout_buffer_pct"], 0.2)
        self.assertAlmostEqual(breakout["high_atr_pct_threshold"], 4.5)
        self.assertAlmostEqual(breakout["high_vol_breakout_buffer_pct"], 0.35)
        self.assertAlmostEqual(breakout["min_rel_volume"], 1.8)
        self.assertAlmostEqual(breakout["max_spread_pct"], 0.35)

    def test_config_remains_public_market_data_only(self):
        config_path = Path("config/frontier_crypto_venues.example.json")
        data = json.loads(config_path.read_text(encoding="utf-8"))

        hard_limits = data["hard_limits"]
        self.assertIn("Public market data only.", hard_limits)
        self.assertIn("No credentials.", hard_limits)
        self.assertIn("No live trading.", hard_limits)


if __name__ == "__main__":
    unittest.main()
import json
from pathlib import Path
import unittest


class FrontierCryptoVenuesConfigTest(unittest.TestCase):
    def test_paper_only_rules_require_complete_json(self):
        config_path = Path("config/frontier_crypto_venues.example.json")
        self.assertTrue(config_path.exists(), "expected frontier crypto venues config to exist")

        data = json.loads(config_path.read_text(encoding="utf-8"))
        paper_rules = data.get("paper_only_rules", {})

        self.assertTrue(paper_rules.get("require_complete_json"))
        self.assertEqual(paper_rules.get("watchlist_promotion"), "two_step_confirm")
        self.assertTrue(paper_rules.get("require_close_above_trigger"))
        self.assertTrue(paper_rules.get("require_next_interval_hold"))
        self.assertEqual(paper_rules.get("assumed_position_size"), "reduced")

    def test_hard_limits_remain_public_data_only(self):
        config_path = Path("config/frontier_crypto_venues.example.json")
        data = json.loads(config_path.read_text(encoding="utf-8"))

        hard_limits = data.get("hard_limits", [])
        self.assertIn("Public market data only.", hard_limits)
        self.assertIn("No live trading.", hard_limits)
        self.assertNotIn("No credentials.", [])

