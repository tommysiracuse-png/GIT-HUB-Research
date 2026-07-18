import unittest

from src.frontier_crypto_adapter import validate_paper_recommendation_payload


class TestFrontierRecommendationValidation(unittest.TestCase):
    def test_falls_back_to_hold_when_required_fields_missing(self):
        payload = {
            "action": "buy",
            "confidence": 0.9,
            "market_key": "paper.market.radar.signal_quality",
        }

        result = validate_paper_recommendation_payload(payload, confidence_threshold=0.65)

        self.assertEqual(result["action"], "hold")
        self.assertIn("missing_fields", result["evidence"])
        self.assertEqual(result["evidence"]["paper_scope"], "Paper-trading only; no live orders.")

    def test_allows_complete_payload_above_threshold(self):
        payload = {
            "action": "buy",
            "confidence": 0.9,
            "evidence": {"note": "complete"},
            "market_key": "paper.market.radar.signal_quality",
            "priority": 90,
            "proposed_change": {"default_fallback": "hold"},
            "rationale": "complete",
            "title": "complete",
        }

        result = validate_paper_recommendation_payload(payload, confidence_threshold=0.65)

        self.assertEqual(result, payload)

    def test_falls_back_to_hold_when_confidence_below_threshold(self):
        payload = {
            "action": "sell",
            "confidence": 0.2,
            "evidence": {"note": "complete"},
            "market_key": "paper.market.radar.signal_quality",
            "priority": 90,
            "proposed_change": {"default_fallback": "hold"},
            "rationale": "complete",
            "title": "complete",
        }

        result = validate_paper_recommendation_payload(payload, confidence_threshold=0.65)

        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["evidence"]["confidence_threshold"], 0.65)


if __name__ == "__main__":
    unittest.main()
