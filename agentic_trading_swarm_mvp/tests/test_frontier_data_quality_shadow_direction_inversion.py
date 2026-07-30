import unittest

try:
    from src.frontier_crypto_adapter import _paper_only_apply_context_inheritance_review
    from src.frontier_data_quality import paper_only_shadow_direction_inversion_review
except ImportError:  # pragma: no cover - fallback for direct execution
    from frontier_crypto_adapter import _paper_only_apply_context_inheritance_review
    from frontier_data_quality import paper_only_shadow_direction_inversion_review


class PaperOnlyShadowDirectionInversionTests(unittest.TestCase):
    def test_review_flips_yahoo_proxy_long_signal_when_enabled(self):
        record = {"signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard"}
        config = {"feature_flags": {"paper_only_shadow_direction_inversion_v1": True}}

        review = paper_only_shadow_direction_inversion_review(record, config=config)

        self.assertTrue(review["enabled"])
        self.assertTrue(review["scope_matched"])
        self.assertTrue(review["applies"])
        self.assertEqual(review["baseline_direction"], "long_proxy")
        self.assertEqual(review["shadow_direction"], "short_proxy")
        self.assertEqual(review["reason"], "shadow_direction_inversion")
        self.assertEqual(review["activation_mode"], "parallel_shadow_only")

    def test_review_ignores_other_signal_families(self):
        record = {"signal_key": "BYBIT|mean_reversion|long|standard"}
        config = {"paper_only_shadow_direction_inversion_v1": True}

        review = paper_only_shadow_direction_inversion_review(record, config=config)

        self.assertTrue(review["enabled"])
        self.assertFalse(review["scope_matched"])
        self.assertFalse(review["applies"])
        self.assertEqual(review["reason"], "scope_mismatch")
        self.assertIsNone(review["shadow_direction"])

    def test_context_review_surfaces_shadow_metadata_without_blocking(self):
        route_status = {
            "signal_key": "YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional",
        }
        config = {"feature_flags": {"paper_only_shadow_direction_inversion_v1": True}}
        base_review = {
            "route_status": "eligible",
            "paper_eligible": True,
            "trade_effect": None,
        }

        review = _paper_only_apply_context_inheritance_review(base_review, route_status, config=config)

        self.assertEqual(review["route_status"], "eligible")
        self.assertTrue(review["paper_eligible"])
        self.assertTrue(review["shadow_direction_inversion_enabled"])
        self.assertTrue(review["shadow_direction_inversion_applies"])
        self.assertEqual(review["shadow_direction_baseline"], "short_proxy")
        self.assertEqual(review["shadow_direction_candidate"], "long_proxy")
        self.assertEqual(review["shadow_direction_variant"], "conditional")
        self.assertEqual(review["shadow_direction_reason"] if "shadow_direction_reason" in review else review["shadow_direction_inversion_reason"], "shadow_direction_inversion")
