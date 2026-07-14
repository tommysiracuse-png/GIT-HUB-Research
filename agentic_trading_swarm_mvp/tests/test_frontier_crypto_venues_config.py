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

