import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import research_worker


class FrontierCashDiscoverySeedTests(unittest.TestCase):
    def _seed_by_venue(self, venue_name: str) -> dict:
        for seed in research_worker.DEFAULT_GLOBAL_DISCOVERY_SEEDS:
            if seed.get("venue_or_source") == venue_name:
                return seed
        self.fail(f"missing discovery seed for {venue_name}")

    def test_jse_seed_is_bounded_and_paper_only(self):
        seed = self._seed_by_venue("Johannesburg Stock Exchange")
        self.assertTrue(seed.get("paper_only"))
        self.assertEqual(seed.get("recommended_next_action"), "adapter_spec")
        self.assertEqual(seed.get("tradability_guess"), "watch_only")
        self.assertIn("paper_only_local_cash_scope", seed.get("route_blockers", []))
        hint = seed.get("adapter_request_hint", {})
        self.assertTrue(hint.get("paper_only"))
        self.assertEqual(hint.get("scope_limit"), "benchmark_or_top_liquid_names_only")
        self.assertEqual(hint.get("route_readiness", {}).get("live_trade_ready"), False)
        normalized_fields = set(hint.get("normalized_fields", []))
        self.assertTrue(
            {"venue", "symbol", "last_price", "timestamp", "freshness_minutes"}.issubset(normalized_fields)
        )

    def test_nse_seed_is_bounded_and_paper_only(self):
        seed = self._seed_by_venue("National Stock Exchange of India")
        self.assertTrue(seed.get("paper_only"))
        self.assertEqual(seed.get("recommended_next_action"), "adapter_spec")
        self.assertEqual(seed.get("tradability_guess"), "watch_only")
        self.assertIn("anti_bot_headers", seed.get("route_blockers", []))
        hint = seed.get("adapter_request_hint", {})
        self.assertTrue(hint.get("paper_only"))
        self.assertEqual(hint.get("scope_limit"), "benchmark_or_top_liquid_names_only")
        self.assertEqual(hint.get("route_readiness", {}).get("live_trade_ready"), False)
        normalized_fields = set(hint.get("normalized_fields", []))
        self.assertTrue(
            {"venue", "symbol", "last_price", "timestamp", "freshness_minutes"}.issubset(normalized_fields)
        )

    def test_frontier_cash_seeds_preserve_bounded_symbol_scope(self):
        venues = {
            "Johannesburg Stock Exchange": {"J200", "J203"},
            "National Stock Exchange of India": {"NIFTY 50", "NIFTY NEXT 50"},
        }
        for venue_name, expected_symbols in venues.items():
            preferred_symbols = set(self._seed_by_venue(venue_name).get("adapter_request_hint", {}).get("preferred_symbols", []))
            self.assertTrue(expected_symbols.issubset(preferred_symbols))


if __name__ == "__main__":
    unittest.main()
