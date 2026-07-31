import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement  # noqa: E402


class StrategyLabValidationTests(unittest.TestCase):
    def test_normalizer_accepts_quality_gate_alias_fields(self):
        normalized = self_improvement._normalize_strategy_lab_recommendation_payload(
            {
                "action": "propose_strategy_lab_experiment",
                "title": "Paper OKX risk filter",
                "proposed_change": "Accept a paper-only OKX funding capture risk_filter with freshness and spread controls.",
                "experiment": {
                    "type": "risk_filter",
                    "market": "okx_perp_funding_basis",
                    "signal": "okx_funding_capture",
                    "spread_bps_max": 7.5,
                    "min_depth_usd": 30000,
                    "min_emit_confidence": 0.58,
                },
            }
        )

        self.assertEqual("quality_gate_experiment", normalized["experiment_type"])
        self.assertEqual("okx_perp_funding_basis", normalized["market_key"])
        self.assertEqual("okx_funding_capture", normalized["signal_key"])
        self.assertEqual(7.5, normalized["entry_gates"]["max_spread_bps"])
        self.assertEqual(30000.0, normalized["entry_gates"]["min_liquidity_usd"])
        self.assertEqual(0.58, normalized["entry_gates"]["min_confidence"])
        self.assertTrue(normalized["consumer_validation"]["normalized_strategy_lab_packet"])

    def test_normalizer_skips_live_expansions(self):
        payload = {
            "action": "propose_strategy_lab_experiment",
            "title": "Live OKX filter",
            "proposed_change": "Create a live trading OKX funding filter and send orders when spread is tight.",
        }
        normalized = self_improvement._normalize_strategy_lab_recommendation_payload(payload)
        self.assertIs(payload, normalized)


if __name__ == "__main__":
    unittest.main()
