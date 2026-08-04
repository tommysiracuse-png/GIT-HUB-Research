from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_bridge import build_cross_context_reliability_cards


class CrossContextReliabilityTests(unittest.TestCase):
    def test_cards_compare_matched_signal_and_context_slices(self) -> None:
        signal_stats = [
            {
                "signal_key": "OKX|perp_funding_basis|funding_capture_long_perp|conditional",
                "closed_count": 33,
                "avg_pnl_bps": 119.8,
                "win_rate": 0.545,
            },
            {
                "signal_key": "OKX|perp_funding_basis|basis_mean_reversion_long_perp|conditional",
                "closed_count": 21,
                "avg_pnl_bps": -352.2,
                "win_rate": 0.476,
            },
            {
                "signal_key": "STRATEGY_LAB|one_off|OKX|long_proxy|standard",
                "closed_count": 100,
                "avg_pnl_bps": 999.0,
                "win_rate": 1.0,
            },
        ]
        contextual_stats = [
            {
                "context_key": "OKX|perp_funding_basis|short_perp_long_spot|standard|basis_magnitude_bucket=extreme",
                "closed_count": 40,
                "avg_pnl_bps": 61.0,
                "win_rate": 0.58,
            },
            {
                "context_key": "OKX|perp_funding_basis|short_perp_long_spot|standard|basis_magnitude_bucket=quiet",
                "closed_count": 45,
                "avg_pnl_bps": -8.0,
                "win_rate": 0.46,
            },
        ]

        result = build_cross_context_reliability_cards(signal_stats, contextual_stats)

        self.assertEqual(result["card_count"], 2)
        groups = {item["group_key"]: item for item in result["cards"]}
        family = groups["OKX|perp_funding_basis"]
        self.assertEqual(family["positive_slice"]["label"], signal_stats[0]["signal_key"])
        self.assertEqual(family["recommended_action"], "preserve_positive_slice_and_diagnose_negative_slice")
        context = groups["OKX|perp_funding_basis|short_perp_long_spot|standard|basis_magnitude_bucket"]
        self.assertEqual(context["positive_slice"]["label"], "extreme")
        self.assertEqual(context["negative_slice"]["label"], "quiet")

    def test_small_or_weak_contrasts_are_not_emitted(self) -> None:
        result = build_cross_context_reliability_cards(
            [
                {"signal_key": "A|B|long|standard", "closed_count": 4, "avg_pnl_bps": 100, "win_rate": 1},
                {"signal_key": "A|B|short|standard", "closed_count": 30, "avg_pnl_bps": 95, "win_rate": 1},
            ],
            [],
        )
        self.assertEqual(result["cards"], [])


if __name__ == "__main__":
    unittest.main()
