import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement  # noqa: E402


class TestOkxFundingCaptureQualityGate(unittest.TestCase):
    def test_wrapper_normalizes_okx_quality_gate_payload_before_ingest(self):
        calls = []
        original = self_improvement._strategy_lab_ingest_strategy_lab_recommendation

        def fake_ingest(payload):
            calls.append(payload)
            return {"status": "accepted", "payload": payload}

        self_improvement._strategy_lab_ingest_strategy_lab_recommendation = fake_ingest
        try:
            result = self_improvement.ingest_strategy_lab_recommendation(
                {
                    "action": "propose_strategy_lab_experiment",
                    "title": "OKX funding capture quality gate",
                    "rationale": "Tighten freshness, spread, liquidity, and confidence filters for OKX perp funding capture.",
                    "type": "risk_filter",
                    "filters": {
                        "freshness_horizon_seconds": 600,
                        "max_entry_spread_bps": 6,
                        "liquidity_floor_usd": 50000,
                        "confidence_floor": 0.62,
                    },
                }
            )
        finally:
            self_improvement._strategy_lab_ingest_strategy_lab_recommendation = original

        self.assertEqual(1, len(calls))
        normalized = calls[0]
        self.assertEqual("quality_gate_experiment", normalized["experiment_type"])
        self.assertTrue(normalized["paper_only"])
        self.assertEqual("okx_perp_funding_basis", normalized["market_key"])
        self.assertEqual("okx_funding_capture", normalized["signal_key"])
        self.assertEqual(600, normalized["entry_gates"]["max_signal_age_seconds"])
        self.assertEqual(6.0, normalized["entry_gates"]["max_spread_bps"])
        self.assertEqual(50000.0, normalized["entry_gates"]["min_liquidity_usd"])
        self.assertEqual(0.62, normalized["entry_gates"]["min_confidence"])
        self.assertIn("basis_mean_reversion", normalized["excluded_modes"])
        self.assertEqual("accepted", result["status"])


if __name__ == "__main__":
    unittest.main()
