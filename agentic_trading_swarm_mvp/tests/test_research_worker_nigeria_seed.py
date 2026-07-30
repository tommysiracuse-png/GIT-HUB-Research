import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import research_worker


class ResearchWorkerNigeriaSeedTests(unittest.TestCase):
    def test_ngx_seed_is_paper_only_and_actionable(self) -> None:
        seed = research_worker.NGX_DIRECT_DISCOVERY_SEED
        self.assertEqual(seed["venue_or_source"], "Nigerian Exchange Group")
        self.assertEqual(seed["country"], "Nigeria")
        self.assertEqual(seed["region"], "West Africa")
        self.assertEqual(seed["adapter_route_id"], "ngx_cash_public_shadow")
        self.assertEqual(seed["recommended_next_action"], "adapter_spec")

        hint = seed["adapter_request_hint"]
        self.assertTrue(hint["paper_only"])
        self.assertEqual(hint["provider_mode"], "public_web_quote_or_delayed_feed")
        self.assertIn(seed, research_worker.DEFAULT_GLOBAL_DISCOVERY_SEEDS)

    def test_ngx_seed_captures_quality_and_freshness_fields(self) -> None:
        seed = research_worker.NGX_DIRECT_DISCOVERY_SEED
        response_fields = set(seed["adapter_request_hint"]["response_fields"])

        self.assertIn("quote_timestamp", response_fields)
        self.assertIn("data_freshness_minutes", response_fields)
        self.assertIn("session_status", response_fields)
        self.assertIn("spread_bps_proxy", response_fields)
        self.assertIn("turnover_proxy", response_fields)
        self.assertIn("DANGCEM", seed["asset_or_event"])
        self.assertIn("GTCO", seed["asset_or_event"])
        self.assertIn("SEPLAT", seed["asset_or_event"])


