import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_crypto_adapter import (  # noqa: E402
    DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY,
    DEFAULT_PAPER_TRADE_POLICY,
    paper_only_executable_quality_check,
    paper_only_venue_direction_expectancy_gate,
)


class TestPaperOnlyVenueDirectionExpectancyGate(unittest.TestCase):
    def test_positive_context_scores_above_neutral(self):
        stats_by_context = {
            "BINANCE_US|long": {
                "closed_trades": 24,
                "recent_expectancy_bps": 18.0,
                "recent_win_rate": 0.62,
                "payoff_ratio": 1.35,
            }
        }

        result = paper_only_venue_direction_expectancy_gate(
            venue="BINANCE_US",
            direction="long",
            stats_by_context=stats_by_context,
        )

        self.assertTrue(result["enabled"])
        self.assertFalse(result["blocked"])
        self.assertGreater(result["score_multiplier"], 1.0)
        self.assertGreaterEqual(result["confidence"], 0.55)

    def test_sparse_context_stays_neutral_without_block(self):
        stats_by_context = {
            "BITSO": {
                "long": {
                    "closed_trades": 3,
                    "recent_expectancy_bps": -25.0,
                    "recent_win_rate": 0.10,
                }
            }
        }

        result = paper_only_venue_direction_expectancy_gate(
            venue="BITSO",
            direction="long",
            stats_by_context=stats_by_context,
        )

        self.assertFalse(result["blocked"])
        self.assertEqual(result["score_multiplier"], 1.0)
        self.assertIn("insufficient_closed_trades", result["reasons"])

    def test_negative_context_blocks_when_confident(self):
        stats_by_context = {
            "MERCADO_BITCOIN|short": {
                "closed_trades": 30,
                "recent_expectancy_bps": -14.0,
                "recent_win_rate": 0.38,
                "payoff_ratio": 0.80,
            }
        }

        result = paper_only_venue_direction_expectancy_gate(
            venue="MERCADO_BITCOIN",
            direction="short",
            stats_by_context=stats_by_context,
        )

        self.assertTrue(result["blocked"])
        self.assertLess(result["score_multiplier"], 1.0)
        self.assertIn("negative_expectancy", result["reasons"])

    def test_executable_quality_check_honors_expectancy_gate(self):
        result = paper_only_executable_quality_check(
            expected_edge_bps=35.0,
            quoted_spread_bps=4.0,
            top_of_book_depth=5000.0,
            paper_order_size=500.0,
            venue_direction_gate={"enabled": True, "blocked": True, "score_multiplier": 0.82, "reasons": ["negative_expectancy"]},
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result["alert_blocked"])
        self.assertIn("venue_direction_expectancy_gate", result["reasons"])
        self.assertIn("venue_direction_expectancy_below_neutral", result["reasons"])

    def test_policy_is_wired_into_default_paper_trade_packet(self):
        self.assertIs(
            DEFAULT_PAPER_TRADE_POLICY["venue_direction_expectancy_gate"],
            DEFAULT_PAPER_ONLY_VENUE_DIRECTION_EXPECTANCY_POLICY,
        )


if __name__ == "__main__":
    unittest.main()
