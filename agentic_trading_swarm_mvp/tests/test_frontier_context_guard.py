import unittest

from src.frontier_crypto_adapter import _paper_only_short_route_review
from src.frontier_data_quality import (
    PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG,
    _paper_only_context_evidence_review,
)


class PaperOnlyContextGuardTests(unittest.TestCase):
    def test_context_guard_disabled_by_default(self):
        review = _paper_only_context_evidence_review(
            {
                "venue": "OKX_SPOT",
                "market_type": "spot",
                "side": "long",
                "leg_structure": "single_leg",
                "carry_bucket": "neutral",
            },
            config={},
        )
        self.assertFalse(review["enabled"])
        self.assertTrue(review["eligible"])
        self.assertEqual(review["variant_state"], "guard_disabled")

    def test_context_guard_allows_positive_exact_match(self):
        config = {
            PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG: True,
            "paper_context_min_sample_size": 20,
            "paper_variant_context_evidence": [
                {
                    "context_signature": {
                        "venue": "OKX_SPOT",
                        "market_type": "spot",
                        "side": "long",
                        "leg_structure": "single_leg",
                        "carry_bucket": "neutral",
                    },
                    "sample_size": 72,
                    "expectancy_bps": 23.1,
                    "approved": True,
                }
            ],
        }
        review = _paper_only_context_evidence_review(
            {
                "venue": "OKX_SPOT",
                "market_type": "spot",
                "side": "long",
                "leg_structure": "single_leg",
                "carry_bucket": "neutral",
            },
            config=config,
        )
        self.assertTrue(review["enabled"])
        self.assertTrue(review["matched"])
        self.assertTrue(review["eligible"])
        self.assertEqual(review["variant_state"], "eligible")
        self.assertEqual(review["reason"], "approved_context_match")
        self.assertEqual(review["inherited_confidence"], 1.0)

    def test_context_guard_demotes_unmatched_context_to_shadow(self):
        config = {
            PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG: True,
            "paper_variant_context_evidence": [
                {
                    "context_signature": {
                        "venue": "OKX_SPOT",
                        "market_type": "spot",
                        "side": "long",
                        "leg_structure": "single_leg",
                        "carry_bucket": "neutral",
                    },
                    "sample_size": 72,
                    "expectancy_bps": 23.1,
                    "approved": True,
                }
            ],
        }
        review = _paper_only_context_evidence_review(
            {
                "venue": "GATE",
                "market_type": "spot",
                "side": "short",
                "leg_structure": "single_leg",
                "carry_bucket": "neutral",
            },
            config=config,
        )
        self.assertTrue(review["enabled"])
        self.assertFalse(review["matched"])
        self.assertFalse(review["eligible"])
        self.assertEqual(review["variant_state"], "paper_shadow_only")
        self.assertEqual(review["reason"], "missing_context_match")
        self.assertEqual(review["inherited_confidence"], 0.0)

    def test_short_route_review_surfaces_shadow_only_demotions(self):
        config = {
            "feature_flags": {
                PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG: True,
            },
            "paper_variant_context_evidence": [
                {
                    "context_signature": {
                        "venue": "OKX_SPOT",
                        "market_type": "spot",
                        "side": "long",
                        "leg_structure": "single_leg",
                        "carry_bucket": "neutral",
                    },
                    "sample_size": 72,
                    "expectancy_bps": 23.1,
                    "approved": True,
                }
            ],
        }
        review = _paper_only_short_route_review(
            {
                "strategy_family": "short_frontier_spot",
                "route_supported": True,
                "paper_mode": "paper",
                "simulated_venue_tag": "paper_sim_only",
                "venue": "GATE",
                "market_type": "spot",
                "side": "short",
                "leg_structure": "single_leg",
                "carry_bucket": "neutral",
            },
            config=config,
        )
        self.assertEqual(review["route_status"], "paper_shadow_only")
        self.assertFalse(review["paper_eligible"])
        self.assertFalse(review["recommendation_eligible"])
        self.assertEqual(review["variant_state"], "paper_shadow_only")
        self.assertEqual(review["context_guard_reason"], "missing_context_match")


if __name__ == "__main__":
    unittest.main()
