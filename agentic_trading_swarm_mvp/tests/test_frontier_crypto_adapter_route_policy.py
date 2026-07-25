import unittest

from src.frontier_crypto_adapter import (
    _paper_only_build_governor_fields,
    _paper_only_short_route_review,
)


class FrontierCryptoAdapterRoutePolicyTests(unittest.TestCase):
    def test_governor_fields_default_to_no_send_paper_metadata(self):
        fields = _paper_only_build_governor_fields()
        self.assertTrue(fields["paper_only"])
        self.assertEqual(fields["execution_mode"], "paper")
        self.assertEqual(fields["paper_mode"], "enforced")
        self.assertEqual(fields["transport"], "no_send")
        self.assertEqual(fields["fill_model"], "synthetic_best_effort")
        self.assertTrue(fields["deny_live_transmit"])
        self.assertEqual(fields["venue_allowlist"], "paper_sim_only")

    def test_route_review_falls_back_when_paper_mode_missing(self):
        config = {"feature_flags": {"paper_only_enforced_route_resolution_v1": True}}
        review = _paper_only_short_route_review(
            {"route_status": "eligible", "simulated_venue_tag": "paper_sim_only"},
            config=config,
        )
        self.assertTrue(review["fallback_applied"])
        self.assertEqual(review["fallback_reason"], "paper_mode_required")
        self.assertEqual(review["route_status"], "eligible")
        self.assertEqual(review["transport"], "no_send")
        self.assertTrue(review["paper_mode"])

    def test_route_review_falls_back_when_simulated_tag_missing(self):
        config = {"feature_flags": {"paper_only_enforced_route_resolution_v1": True}}
        review = _paper_only_short_route_review(
            {"route_status": "eligible", "paper_mode": "enforced", "venue": "OKX", "transport": "rest"},
            config=config,
        )
        self.assertTrue(review["fallback_applied"])
        self.assertEqual(review["fallback_reason"], "missing_simulated_venue_tag")
        self.assertEqual(review["simulated_venue_tag"], "paper_sim_only")
        self.assertEqual(review["fill_model"], "synthetic_best_effort")
        self.assertTrue(review["deny_live_transmit"])

    def test_route_review_preserves_explicit_simulated_route(self):
        config = {"feature_flags": {"paper_only_enforced_route_resolution_v1": True}}
        review = _paper_only_short_route_review(
            {
                "route_status": "eligible",
                "paper_mode": "enforced",
                "simulated_venue_tag": "paper_sim_only",
                "transport": "no_send",
                "fill_model": "synthetic_best_effort",
            },
            config=config,
        )
        self.assertFalse(review["fallback_applied"])
        self.assertEqual(review["route_status"], "eligible")
        self.assertEqual(review["transport"], "no_send")


if __name__ == "__main__":
    unittest.main()
