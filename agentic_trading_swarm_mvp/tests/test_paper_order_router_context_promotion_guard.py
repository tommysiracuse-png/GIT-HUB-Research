import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_order_router import _apply_route_feasibility_metadata, frontier_shadow_filter_reason


class PaperOrderRouterContextPromotionGuardTests(unittest.TestCase):
    def test_unconfirmed_translation_remains_observable_not_shadow_filtered(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "coinbase",
                "direction": "short",
                "trade_family": "basis_carry",
            },
        }

        reason = frontier_shadow_filter_reason(candidate)
        annotated = _apply_route_feasibility_metadata(candidate)

        self.assertIsNone(reason)
        self.assertTrue(annotated["paper_observation_only"])
        self.assertTrue(annotated["paper_fill_allowed_by_route"])
        self.assertFalse(annotated["paper_context_promotion_eligible"])
        self.assertEqual(0.15, annotated["paper_score_multiplier"])

    def test_apply_route_feasibility_metadata_attaches_context_guard_annotation(self):
        candidate = {
            "venue": "kraken",
            "direction": "long",
            "signal_family": "frontier_spot_alpha",
            "promotion_source_context": {
                "venue": "kraken",
                "direction": "long",
                "trade_family": "frontier_spot_alpha",
            },
        }

        annotated = _apply_route_feasibility_metadata(candidate)

        self.assertIn("paper_context_promotion_guard", annotated)
        self.assertEqual(
            annotated["paper_context_promotion_guard_key"],
            "paper_route_lineage_confirmation",
        )
        self.assertTrue(annotated["paper_context_promotion_eligible"])
        self.assertFalse(annotated["paper_context_promotion_blocked"])


if __name__ == "__main__":
    unittest.main()
