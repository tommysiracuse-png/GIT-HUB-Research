import unittest

from src.frontier_crypto_adapter import paper_only_cross_market_review_state


class TestPaperOnlyCrossMarketReviewState(unittest.TestCase):
    def test_incomplete_evidence_defaults_to_observe_only(self):
        result = paper_only_cross_market_review_state(
            evidence={
                "data_quality": "previous response invalid or incomplete",
                "execution_scope": "paper only",
            },
            signal_state="inconclusive",
        )

        self.assertEqual(result["paper_review_state"], "observe_only")
        self.assertEqual(result["portfolio_action"], "no position change")
        self.assertEqual(result["sizing"], "0 simulated allocation change")
        self.assertIn("risk_view", result["missing_evidence_fields"])

    def test_complete_evidence_can_pass_review(self):
        result = paper_only_cross_market_review_state(
            evidence={
                "data_quality": "complete",
                "execution_scope": "paper only",
                "risk_view": "elevated model uncertainty",
                "signal_state": "validated",
            },
            signal_state="validated",
        )

        self.assertEqual(result["paper_review_state"], "review_ok")
        self.assertEqual(result["portfolio_action"], "paper candidate eligible")
        self.assertEqual(result["sizing"], "paper-sized allocation permitted")


if __name__ == "__main__":
    unittest.main()
