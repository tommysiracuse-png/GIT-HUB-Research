import unittest

from src.frontier_crypto_adapter import paper_only_execution_mode_guard, paper_only_radar_alert_gate


class PaperOnlyExecutionModeGuardTests(unittest.TestCase):
    def test_guard_allows_paper_execution_mode(self):
        payload = {
            "execution_mode": "paper",
            "symbol": "BTC/USDT",
            "buy_venue": "BINANCE",
            "sell_venue": "KRAKEN",
        }
        result = paper_only_execution_mode_guard(payload)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["status"], "eligible")
        self.assertTrue(result["submission_allowed"])
        self.assertEqual(result["execution_mode"], "paper")
        self.assertEqual(
            result["review_context"],
            {
                "symbol": "BTC/USDT",
                "buy_venue": "BINANCE",
                "sell_venue": "KRAKEN",
            },
        )

    def test_guard_blocks_non_paper_execution_and_keeps_review_context(self):
        payload = {
            "execution_mode": "live",
            "symbol": "ETH/USDT",
            "route_key": "BINANCE->KRAKEN",
            "side": "buy",
        }
        result = paper_only_execution_mode_guard(payload)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "execution_mode_blocked")
        self.assertTrue(result["route_evaluation_allowed"])
        self.assertFalse(result["submission_allowed"])
        self.assertEqual(result["review_context"]["route_key"], "BINANCE->KRAKEN")
        self.assertEqual(result["review_context"]["symbol"], "ETH/USDT")

    def test_radar_alert_gate_accepts_legacy_paper_mode(self):
        result = paper_only_radar_alert_gate({"paper_mode": True, "confidence": 0.9})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "eligible")
        self.assertTrue(result["paper_mode"])
        self.assertIsNotNone(result["execution_guard"])
        self.assertEqual(result["execution_guard"]["execution_mode"], "paper")

    def test_radar_alert_gate_blocks_live_execution_mode(self):
        result = paper_only_radar_alert_gate(
            {
                "execution_mode": "live",
                "paper_mode": True,
                "confidence": 0.95,
                "symbol": "SOL/USDT",
            }
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "execution_mode_blocked")
        self.assertEqual(result["execution_guard"]["review_context"]["symbol"], "SOL/USDT")
