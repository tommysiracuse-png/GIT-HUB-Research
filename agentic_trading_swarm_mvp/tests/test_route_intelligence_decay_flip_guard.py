import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from route_intelligence import build_conditional_paper_quality_gate


class ConditionalPaperQualityGateDecayFlipGuardTests(unittest.TestCase):
    def test_conditional_short_decay_flip_guard_triggers_on_negative_expanded_sample(self) -> None:
        report = build_conditional_paper_quality_gate(
            [
                {
                    "market_key": "OKX_SPOT|frontier_crypto_venue_map|short_frontier_spot|conditional",
                    "inst_id": "OKX:ARC-USDT",
                    "venue": "OKX",
                    "direction": "short",
                    "route_status": "conditional",
                    "prior_count": 7,
                    "prior_avg_bps": 45.735,
                    "paper_count": 29,
                    "rolling_expectancy_bps": -4.2,
                    "paper_drawdown_bps": 31.0,
                }
            ]
        )

        self.assertEqual(report["gate_count"], 1)
        self.assertEqual(report["reason_counts"]["decay_flip_guard_non_admissible"], 1)
        example = report["top_examples"][0]
        self.assertEqual(example["paper_policy_action"], "score_clamped_non_admissible")
        self.assertTrue(example["paper_policy_guard"]["triggered"])
        self.assertFalse(example["paper_policy_guard"]["exploit_more_eligible"])
        self.assertEqual(example["paper_policy_guard"]["cooldown_cycles_remaining"], 12)

    def test_conditional_short_requires_expanded_sample_confirmation_before_continuation(self) -> None:
        report = build_conditional_paper_quality_gate(
            [
                {
                    "market_key": "OKX_SPOT|frontier_crypto_venue_map|short_frontier_spot|conditional",
                    "inst_id": "OKX:ARC-USDT",
                    "venue": "OKX",
                    "direction": "short",
                    "route_status": "conditional",
                    "prior_count": 7,
                    "prior_avg_bps": 45.735,
                    "paper_count": 12,
                    "rolling_expectancy_bps": 6.0,
                    "paper_drawdown_bps": 4.0,
                }
            ]
        )

        self.assertEqual(report["gate_count"], 1)
        self.assertEqual(report["reason_counts"]["expanded_sample_confirmation_pending"], 1)
        example = report["top_examples"][0]
        self.assertEqual(example["paper_policy_action"], "hold_conditional_paper_only")
        self.assertFalse(example["paper_policy_guard"]["triggered"])
        self.assertFalse(example["paper_policy_guard"]["exploit_more_eligible"])
        self.assertEqual(example["paper_policy_guard"]["confirmation_progress_count"], 12)

    def test_long_direction_is_not_caught_by_short_decay_flip_guard(self) -> None:
        report = build_conditional_paper_quality_gate(
            [
                {
                    "inst_id": "OKX:ARC-USDT",
                    "venue": "OKX",
                    "direction": "long",
                    "route_status": "conditional",
                    "prior_count": 7,
                    "prior_avg_bps": 45.735,
                    "paper_count": 29,
                    "rolling_expectancy_bps": -4.2,
                    "paper_drawdown_bps": 31.0,
                }
            ]
        )

        self.assertEqual(report["gate_count"], 0)
