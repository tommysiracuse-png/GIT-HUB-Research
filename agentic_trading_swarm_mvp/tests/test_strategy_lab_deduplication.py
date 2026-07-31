import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement  # noqa: E402


class StrategyLabDeduplicationTests(unittest.TestCase):
    def test_near_duplicate_okx_quality_gate_payloads_share_canonical_key(self):
        payload_a = {
            "action": "propose_strategy_lab_experiment",
            "title": "OKX funding capture quality gate",
            "rationale": "Tighten freshness, spread, liquidity, and confidence gates for paper-only funding capture.",
            "filters": {
                "freshness_horizon_seconds": 900,
                "max_entry_spread_bps": 8,
                "liquidity_floor_usd": 25000,
                "confidence_floor": 0.55,
                "disabled_variants": ["basis_mean_reversion", "spot_leg", "spot_carry"],
            },
        }
        payload_b = {
            "action": "propose_strategy_lab_experiment",
            "title": "OKX risk_filter experiment",
            "proposed_change": "Use a quality_gate_experiment for okx perp funding basis entries only.",
            "experiment": {
                "type": "risk_filter",
                "market": "okx_perp_funding_basis",
                "signal": "okx_funding_capture",
                "max_signal_age_seconds": 900,
                "spread_bps_max": 8,
                "min_liquidity_usd": 25000,
                "min_confidence": 0.55,
                "exclude_modes": ["basis_mean_reversion", "spot_leg", "spot_carry"],
            },
        }

        normalized_a = self_improvement._normalize_strategy_lab_recommendation_payload(payload_a)
        normalized_b = self_improvement._normalize_strategy_lab_recommendation_payload(payload_b)

        self.assertEqual(normalized_a["canonical_key"], normalized_b["canonical_key"])
        self.assertEqual(normalized_a["entry_gates"], normalized_b["entry_gates"])
        self.assertEqual(normalized_a["excluded_modes"], normalized_b["excluded_modes"])


if __name__ == "__main__":
    unittest.main()
