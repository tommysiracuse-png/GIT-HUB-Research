import unittest

from src.frontier_crypto_adapter import (
    _paper_only_frontier_route_feasibility_gate_review,
    _paper_only_frontier_route_quality_gate_review,
)


class PaperOnlyFrontierRouteFeasibilityGateTests(unittest.TestCase):
    def test_blocks_paper_shadow_only_spot_short_route(self):
        review = _paper_only_frontier_route_feasibility_gate_review(
            {
                "route_type": "spot_short_with_borrow",
                "venue": "okx",
                "route_requirements": {
                    "spot_short_support": "paper_shadow_only",
                    "margin_support": "supported",
                    "borrow_reference": "public_borrow_proxy",
                },
            }
        )

        self.assertIsInstance(review, dict)
        self.assertTrue(review["blocked"])
        self.assertFalse(review["eligible"])
        self.assertEqual(review["route_classification"], "spot_short_with_borrow")
        self.assertEqual(review["reason"], "spot_short_route_unsupported")

    def test_blocks_unknown_spot_short_route_support(self):
        review = _paper_only_frontier_route_feasibility_gate_review(
            {
                "route_type": "spot_short_with_borrow",
                "venue": "frontier_unknown",
                "route_requirements": {},
            }
        )

        self.assertIsInstance(review, dict)
        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "spot_short_route_support_unknown")

    def test_supported_basis_route_remains_eligible(self):
        review = _paper_only_frontier_route_feasibility_gate_review(
            {
                "route_type": "spot_plus_perp",
                "venue": "okx",
                "route_requirements": {
                    "basis_support": "supported",
                    "perp_support": "supported",
                    "fee_reference": "public_taker_fee_schedule",
                },
            }
        )

        self.assertIsInstance(review, dict)
        self.assertFalse(review["blocked"])
        self.assertTrue(review["eligible"])
        self.assertEqual(review["route_classification"], "spot_plus_perp")
        self.assertEqual(review["reason"], "route_supported")

    def test_quality_gate_short_circuits_on_feasibility_block(self):
        review = _paper_only_frontier_route_quality_gate_review(
            {
                "route_type": "spot_short_with_borrow",
                "venue": "okx",
                "route_requirements": {
                    "spot_short_support": "unsupported",
                    "margin_support": "supported",
                    "borrow_reference": "public_borrow_proxy",
                },
            }
        )

        self.assertIsInstance(review, dict)
        self.assertTrue(review["blocked"])
        self.assertEqual(review["reason"], "spot_short_route_unsupported")


if __name__ == "__main__":
    unittest.main()
