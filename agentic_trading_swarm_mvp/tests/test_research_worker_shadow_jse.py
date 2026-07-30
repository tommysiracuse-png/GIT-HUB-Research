import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_bridge import GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS
from research_worker import DEFAULT_GLOBAL_DISCOVERY_SEEDS


class TestJseShadowDiscoverySeed(unittest.TestCase):
    def test_jse_seed_requests_venue_native_shadow_fields(self):
        jse_seed = next(
            seed
            for seed in DEFAULT_GLOBAL_DISCOVERY_SEEDS
            if seed.get("venue_or_source") == "Johannesburg Stock Exchange"
        )
        self.assertEqual(jse_seed.get("adapter_route_id"), "jse_cash_public_shadow")
        hint = jse_seed["adapter_request_hint"]
        self.assertTrue(hint["paper_only"])
        self.assertEqual(hint["shadow_mode"]["baseline"], "proxy_generated_jse_candidates")
        self.assertIn("venue_native_symbol", hint["normalized_fields"])
        self.assertIn("session_timestamp", hint["normalized_fields"])
        self.assertIn("turnover_proxy", hint["normalized_fields"])
        self.assertIn("quote_quality", hint["normalized_fields"])
        self.assertIn("top_mover_rank", hint["normalized_fields"])
        self.assertTrue(hint["top_mover_discovery"]["enabled"])
        self.assertTrue(hint["quote_quality_preferences"]["session_timestamp_required"])

    def test_implemented_term_list_covers_shadow_jse_language(self):
        required = {
            "johannesburg stock exchange",
            "venue-native",
            "shadow comparison",
            "turnover proxy",
            "session timestamp",
            "top movers",
        }
        self.assertTrue(required.issubset(set(GLOBAL_MARKET_DISCOVERY_IMPLEMENTED_TERMS)))
