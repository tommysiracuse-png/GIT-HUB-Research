import unittest

from src.frontier_crypto_adapter import _paper_only_route_confidence


class PaperOnlyFrontierRouteQualityGateConfidenceTests(unittest.TestCase):
    def test_quality_gate_preserves_supported_route_confidence(self):
        confidence = _paper_only_route_confidence(
            {
                "route_count": 3,
                "liquidity_usd": 125000.0,
                "spread_pct": 0.25,
                "quote_age_seconds": 8.0,
                "market_quality_score": 0.92,
                "route_confidence": 0.85,
            }
        )
        self.assertEqual(confidence, 0.85)

    def test_quality_gate_blocks_failed_route_thresholds(self):
        confidence = _paper_only_route_confidence(
            {
                "route_count": 3,
                "liquidity_usd": 125000.0,
                "spread_pct": 1.75,
                "quote_age_seconds": 8.0,
                "market_quality_score": 0.92,
                "route_confidence": 0.85,
            }
        )
        self.assertEqual(confidence, 0.0)

    def test_incomplete_quality_evidence_falls_back_to_neutral_confidence(self):
        confidence = _paper_only_route_confidence(
            {
                "route_count": 3,
                "liquidity_usd": 125000.0,
                "route_confidence": 0.95,
            }
        )
        self.assertEqual(confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
