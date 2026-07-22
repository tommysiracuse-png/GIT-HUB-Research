import unittest

from src.code_evolution import (
    STRICT_RECOMMENDATION_MARKET_KEY,
    _normalize_paper_variant_config,
    _sanitize_recommendation_object,
)


class CodeEvolutionSafetyTests(unittest.TestCase):
    def test_normalize_paper_variant_config_enforces_paper_defaults(self) -> None:
        normalized = _normalize_paper_variant_config({"mode": "live", "allow_live_execution": "true"})
        self.assertEqual(normalized["mode"], "paper")
        self.assertEqual(normalized["allow_live_execution"], "false")
        self.assertEqual(normalized["routing_preference"], "higher_fill_probability_over_micro_price_improvement")
        self.assertEqual(normalized["max_order_notional"], "1000")
        self.assertEqual(normalized["slippage_bps_cap"], "5")

    def test_sanitize_recommendation_object_adds_paper_scoped_market_and_variant(self) -> None:
        sanitized = _sanitize_recommendation_object({"title": "x"})
        self.assertEqual(sanitized["market_key"], STRICT_RECOMMENDATION_MARKET_KEY)
        self.assertEqual(sanitized["variant_config"]["mode"], "paper")
        self.assertEqual(sanitized["variant_config"]["allow_live_execution"], "false")


if __name__ == "__main__":
    unittest.main()
