import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_order_router import _apply_route_feasibility_metadata, frontier_shadow_filter_reason


class PaperOrderRouterContextPromotionGuardTests(unittest.TestCase):
    def test_shadow_filter_returns_context_promotion_reason_for_mismatch(self):
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

        self.assertIsNotNone(reason)
        self.assertEqual(reason["reason"], "paper_context_promotion_mismatch")
        self.assertEqual(reason["guard"], "paper_context_promotion_scope")
        self.assertFalse(reason["paper_fill_allowed"])
        self.assertEqual(set(reason["mismatched_fields"]), {"venue", "direction", "trade_family"})

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
            "paper_context_promotion_scope",
        )
        self.assertTrue(annotated["paper_context_promotion_eligible"])
        self.assertFalse(annotated["paper_context_promotion_blocked"])


if __name__ == "__main__":
    unittest.main()
