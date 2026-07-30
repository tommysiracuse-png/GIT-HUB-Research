import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_bridge import GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS
from research_worker import DEFAULT_GLOBAL_DISCOVERY_SEEDS


class TestJSEPaperDiscoverySeed(unittest.TestCase):
    def _jse_seed(self):
        for item in DEFAULT_GLOBAL_DISCOVERY_SEEDS:
            if str(item.get("venue_or_source") or "").strip().lower() == "johannesburg stock exchange":
                return item
        self.fail("Johannesburg Stock Exchange seed not found")

    def test_jse_seed_has_paper_only_shadow_and_quality_gates(self):
        seed = self._jse_seed()
        hint = seed.get("adapter_request_hint") or {}
        shadow_mode = hint.get("shadow_mode") or {}
        quality_gates = hint.get("quality_gates") or {}
        scoring_policy = hint.get("scoring_policy") or {}
        expansion_policy = hint.get("expansion_policy") or {}

        self.assertTrue(seed.get("paper_only"))
        self.assertEqual(seed.get("adapter_route_id"), "jse_cash_public_shadow")
        self.assertTrue(hint.get("paper_only"))
        self.assertTrue(shadow_mode.get("enabled"))
        self.assertEqual(shadow_mode.get("shadow_compare_days"), 14)
        self.assertEqual(
            shadow_mode.get("baseline_signal_key"),
            "JOHANNESBURG_STOCK_EXCHANGE|global_market_discovery_proxy|long_proxy|standard",
        )
        self.assertEqual(quality_gates.get("maximum_freshness_minutes"), 20)
        self.assertEqual(quality_gates.get("max_spread_bps"), 250.0)
        self.assertTrue(quality_gates.get("quote_delay_allowed"))
        self.assertEqual(
            quality_gates.get("required_fields"),
            ["session_timestamp", "freshness_minutes"],
        )
        self.assertEqual(
            quality_gates.get("liquidity_gate"),
            {"require_any": ["turnover_proxy", "volume_proxy"], "min_present_fields": 1},
        )
        self.assertEqual(scoring_policy.get("channel"), "paper_discovery_only")
        self.assertTrue(scoring_policy.get("paper_only"))
        self.assertFalse(scoring_policy.get("use_for_live_trades"))
        self.assertTrue(expansion_policy.get("broader_africa_venue_expansion_blocked"))
        self.assertTrue(expansion_policy.get("requires_quality_gate_pass"))

    def test_llm_bridge_detects_new_jse_implementation_markers(self):
        for term in ("quality_gates", "maximum_freshness_minutes", "paper_discovery_only", "requires_quality_gate_pass"):
            self.assertIn(term, GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS)


if __name__ == "__main__":
    unittest.main()
