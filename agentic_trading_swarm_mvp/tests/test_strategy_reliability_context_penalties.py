import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import strategy_reliability as sr


class StrategyReliabilityContextPenaltyTests(unittest.TestCase):
    def test_context_penalties_disabled_by_default(self) -> None:
        candidate = {
            "score": 50.0,
            "symbol": "BTC/USD",
            "trade_type": "paper_cross_market_radar",
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["baseline"])

        self.assertEqual(candidate["score"], 50.0)
        self.assertNotIn("paper_score_context_penalty", candidate)
        self.assertNotIn("paper_score_context_penalty", reliability)

    def test_context_penalties_reduce_score_and_log_components(self) -> None:
        candidate = {
            "score": 100.0,
            "symbol": "BTC/USD",
            "trade_type": "paper_cross_market_radar",
            "paper_only_context_penalties": True,
            "supporting_market_data_age_seconds": 120.0,
            "supporting_market_refresh_window_seconds": 60.0,
            "realized_volatility": 0.08,
            "volatility_stress_threshold": 0.04,
            "correlated_markets_confirmed": False,
            "market_breadth_confirmed": False,
            "independent_feature_count": 1,
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["penalized"])
        detail = candidate["paper_score_context_penalty"]

        self.assertAlmostEqual(detail["base_score"], 100.0, places=3)
        self.assertAlmostEqual(detail["total_multiplier"], 0.522006, places=6)
        self.assertAlmostEqual(candidate["score"], 52.201, places=3)
        self.assertEqual(detail["dominant_penalty_reason"], "freshness_penalty")
        self.assertEqual(detail["terms"]["freshness_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["regime_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["confirmation_penalty"]["multiplier"], 0.85)
        self.assertEqual(detail["terms"]["concentration_penalty"]["multiplier"], 0.85)
        self.assertEqual(reliability["paper_score_context_penalty"]["final_score"], 52.201)
        self.assertIn("paper_context_penalty:freshness_penalty", candidate["risk_notes"])

    def test_context_penalties_are_neutral_when_inputs_are_missing(self) -> None:
        candidate = {
            "score": 80.0,
            "symbol": "ETH/USD",
            "trade_type": "paper_cross_market_radar",
            "paper_only_context_penalties": True,
        }

        reliability = sr._annotate(candidate, profile="test_profile", action="observe", reasons=["neutral"])
        detail = candidate["paper_score_context_penalty"]

        self.assertEqual(candidate["score"], 80.0)
        self.assertEqual(detail["total_multiplier"], 1.0)
        self.assertIsNone(detail["dominant_penalty_reason"])
        self.assertFalse(detail["applied"])
        self.assertEqual(reliability["paper_score_context_penalty"]["base_score"], 80.0)
        self.assertNotIn("paper_context_penalty:freshness_penalty", candidate.get("risk_notes", []))


if __name__ == "__main__":
    unittest.main()
