import unittest

from src.frontier_crypto_adapter import _paper_only_frontier_route_quality_gate_review


class PaperOnlyStrategyLabSurfaceLineageGateTests(unittest.TestCase):
    def test_non_strategy_lab_quality_review_remains_unchanged(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "route_count": 3,
                "liquidity_usd": 50000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 12.0,
                "market_quality_score": 0.8,
            }
        )

        self.assertTrue(review["eligible"])
        self.assertFalse(review["blocked"])
        self.assertEqual(review["reason"], "quality_thresholds_satisfied")
        self.assertNotIn("surface_lineage_review", review)

    def test_strategy_lab_payload_without_lineage_metadata_is_blocked(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "lab_id": "lab-123",
                "route_count": 3,
                "liquidity_usd": 50000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 12.0,
                "market_quality_score": 0.8,
            }
        )

        self.assertFalse(review["eligible"])
        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "missing_lineage_metadata")

    def test_cross_surface_transfer_requires_explicit_policy(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "lab_id": "lab-123",
                "origin_surface": "yahoo",
                "candidate_surface": "frontier_spot",
                "route_count": 3,
                "liquidity_usd": 50000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 12.0,
                "market_quality_score": 0.8,
            }
        )

        self.assertFalse(review["eligible"])
        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "cross_surface_requires_explicit_transfer_policy")

    def test_explicit_cross_surface_transfer_must_pass_freshness_liquidity_and_behavior(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "lab_id": "lab-123",
                "origin_surface": "yahoo",
                "candidate_surface": "frontier_spot",
                "transfer_policy": "explicit",
                "route_count": 3,
                "liquidity_usd": 55000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 15.0,
                "market_quality_score": 0.8,
                "behavior_consistency": 0.9,
            }
        )

        self.assertTrue(review["eligible"])
        self.assertFalse(review["blocked"])
        self.assertEqual(review["reason"], "cross_surface_transfer_validated")
        self.assertEqual(review["surface_lineage_review"]["reason"], "cross_surface_transfer_validated")

    def test_explicit_cross_surface_transfer_fails_closed_when_checks_fail(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "lab_id": "lab-123",
                "origin_surface": "yahoo",
                "candidate_surface": "frontier_spot",
                "transfer_policy": "explicit",
                "route_count": 3,
                "liquidity_usd": 55000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 120.0,
                "market_quality_score": 0.8,
            }
        )

        self.assertFalse(review["eligible"])
        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "cross_surface_transfer_checks_failed")
