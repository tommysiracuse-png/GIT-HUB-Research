import unittest

from src import frontier_crypto_adapter as adapter


class PaperOnlyShadowDirectionInversionTests(unittest.TestCase):
    def test_negative_expectancy_yahoo_proxy_family_publishes_shadow_inverse(self):
        review = adapter._paper_only_shadow_direction_inversion_review(
            {
                "signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                "side": "buy",
            },
            context_review={
                "sample_size": 176,
                "min_sample_size": 20,
                "expectancy_bps": -16.225,
                "context_signature_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            },
            config={
                "feature_flags": {
                    "paper_only_shadow_direction_inversion_v1": True,
                }
            },
        )

        self.assertTrue(review["enabled"])
        self.assertTrue(review["eligible"])
        self.assertEqual(review["market_key"], "YAHOO_PROXY")
        self.assertEqual(review["signal_family"], "global_proxy_momentum")
        self.assertEqual(review["baseline_direction"], "long")
        self.assertEqual(review["shadow_direction"], "short")
        self.assertTrue(review["publish_shadow_candidate"])
        self.assertTrue(review["preserve_baseline"])
        self.assertEqual(review["activation_mode"], "paper_shadow_parallel")
        self.assertEqual(review["reason"], "negative_expectancy_shadow_inverse")

    def test_positive_expectancy_stays_observe_only(self):
        review = adapter._paper_only_shadow_direction_inversion_review(
            {
                "signal_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
                "side": "buy",
            },
            context_review={
                "sample_size": 176,
                "min_sample_size": 20,
                "expectancy_bps": 1.5,
                "context_signature_key": "YAHOO_PROXY|global_proxy_momentum|long_proxy|standard",
            },
            config={
                "feature_flags": {
                    "paper_only_shadow_direction_inversion_v1": True,
                }
            },
        )

        self.assertTrue(review["enabled"])
        self.assertFalse(review["eligible"])
        self.assertFalse(review["publish_shadow_candidate"])
        self.assertEqual(review["reason"], "non_negative_expectancy")

    def test_context_inheritance_review_surfaces_shadow_metadata(self):
        base_review = {"route_status": "eligible"}
        route_status = {
            "signal_key": "YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional",
        }
        config = {"feature_flags": {"paper_only_shadow_direction_inversion_v1": True}}

        with unittest.mock.patch.object(
            adapter,
            "_paper_only_context_evidence_review",
            return_value={"enabled": True, "eligible": True, "sample_size": 171, "expectancy_bps": -24.614, "context_signature_key": "YAHOO_PROXY|global_proxy_momentum|short_proxy|conditional"},
        ):
            review = adapter._paper_only_apply_context_inheritance_review(base_review, route_status, config=config)

        self.assertEqual(review["shadow_variant"], "shadow_direction_inversion")
        self.assertEqual(review["shadow_direction"], "long")
        self.assertTrue(review["shadow_publish"])
