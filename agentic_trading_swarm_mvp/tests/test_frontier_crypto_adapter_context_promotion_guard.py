import unittest

try:
    from src.frontier_crypto_adapter import _paper_only_strategy_lab_context_transfer_review
except ImportError:  # pragma: no cover - direct module execution fallback
    from frontier_crypto_adapter import _paper_only_strategy_lab_context_transfer_review


class PaperOnlyStrategyLabContextPromotionGuardTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "feature_flags": {
                "paper_only_strategy_lab_exact_context_promotion_v1": True,
            }
        }
        self.context_review = {
            "enabled": True,
            "matched": True,
            "eligible": True,
            "sample_size": 25,
            "min_sample_size": 20,
        }

    def test_blocks_promotion_when_required_context_field_mismatches(self):
        route_status = {
            "recommendation_source": "strategy_lab",
            "source_venue_class": "centralized_exchange",
            "target_venue_class": "centralized_exchange",
            "source_market_surface": "spot",
            "target_market_surface": "spot",
            "source_direction": "long",
            "target_direction": "long",
            "source_data_source_class": "direct_exchange",
            "target_data_source_class": "proxy_feed",
        }

        review = _paper_only_strategy_lab_context_transfer_review(
            route_status,
            context_review=self.context_review,
            config=self.config,
        )

        self.assertTrue(review["enabled"])
        self.assertTrue(review["exact_context_required"])
        self.assertTrue(review["block_promotion"])
        self.assertEqual(review["reason"], "strategy_lab_exact_context_mismatch")
        self.assertIn("data_source_class", review["mismatch_fields"])
        self.assertEqual(review["promotion_delta_scale"], 0.0)

    def test_blocks_promotion_when_required_context_fields_are_missing(self):
        route_status = {
            "recommendation_source": "strategy_lab",
            "source_venue_class": "centralized_exchange",
            "target_venue_class": "centralized_exchange",
            "source_market_surface": "spot",
            "target_market_surface": "spot",
            "source_direction": "long",
            "target_direction": "long",
            "source_data_source_class": "direct_exchange",
        }

        review = _paper_only_strategy_lab_context_transfer_review(
            route_status,
            context_review=self.context_review,
            config=self.config,
        )

        self.assertTrue(review["block_promotion"])
        self.assertEqual(review["reason"], "strategy_lab_exact_context_incomplete")
        self.assertIn("target:data_source_class", review["missing_fields"])
        self.assertEqual(review["promotion_delta_scale"], 0.0)

    def test_blocks_promotion_when_sample_size_is_below_guardrail(self):
        route_status = {
            "recommendation_source": "strategy_lab",
            "source_venue_class": "centralized_exchange",
            "target_venue_class": "centralized_exchange",
            "source_market_surface": "spot",
            "target_market_surface": "spot",
            "source_direction": "long",
            "target_direction": "long",
            "source_data_source_class": "direct_exchange",
            "target_data_source_class": "direct_exchange",
        }
        context_review = dict(self.context_review, sample_size=5, min_sample_size=20)

        review = _paper_only_strategy_lab_context_transfer_review(
            route_status,
            context_review=context_review,
            config=self.config,
        )

        self.assertTrue(review["block_promotion"])
        self.assertFalse(review["sample_guard_passed"])
        self.assertEqual(review["reason"], "strategy_lab_context_evidence_sample_guard")
        self.assertEqual(review["promotion_delta_scale"], 0.0)

    def test_allows_promotion_only_for_exact_context_with_sufficient_sample(self):
        route_status = {
            "recommendation_source": "strategy_lab",
            "source_venue_class": "centralized_exchange",
            "target_venue_class": "centralized_exchange",
            "source_market_surface": "spot",
            "target_market_surface": "spot",
            "source_direction": "long",
            "target_direction": "long",
            "source_data_source_class": "direct_exchange",
            "target_data_source_class": "direct_exchange",
        }

        review = _paper_only_strategy_lab_context_transfer_review(
            route_status,
            context_review=self.context_review,
            config=self.config,
        )

        self.assertTrue(review["exact_context_required"])
        self.assertFalse(review["block_promotion"])
        self.assertTrue(review["sample_guard_passed"])
        self.assertEqual(review["promotion_delta_scale"], 1.0)
