import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import research_worker


class ResearchWorkerSaudiExchangeSeedTests(unittest.TestCase):
    def test_saudi_exchange_seed_is_bounded_and_paper_safe(self) -> None:
        seed = next(
            (
                item
                for item in research_worker.DEFAULT_GLOBAL_DISCOVERY_SEEDS
                if item.get("venue_or_source") == "Saudi Exchange"
            ),
            None,
        )
        self.assertIsNotNone(seed)
        self.assertEqual(seed["surface_type_raw"], "cash equity benchmark and top-liquid constituent public market data")
        self.assertEqual(seed["region"], "MENA")
        self.assertEqual(seed["data_access_type"], "public_no_key")
        self.assertEqual(seed["tradability_guess"], "route_needed")
        self.assertEqual(seed["recommended_next_action"], "adapter_spec")
        self.assertEqual(seed.get("adapter_route_id"), "saudi_exchange_cash_public_shadow")
        self.assertIn("bounded", seed["inefficiency_hypothesis"].lower())
        hint = seed.get("adapter_request_hint") or {}
        self.assertEqual(hint.get("universe_policy"), "bounded_index_and_top_liquid_constituents")
        self.assertEqual(hint.get("max_constituents"), 20)
        self.assertIn("freshness_proxy_validation", seed.get("route_blockers") or [])
        self.assertIn("spread_proxy_validation", seed.get("route_blockers") or [])
        self.assertIn("Tadawul All Share Index", seed["asset_or_event"])
