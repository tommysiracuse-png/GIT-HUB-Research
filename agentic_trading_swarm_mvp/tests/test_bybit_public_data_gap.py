import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_bridge
import research_worker


class BybitPublicDataGapTests(unittest.TestCase):
    def test_bybit_spot_seed_is_available_for_adapter_specs(self):
        bybit_candidates = [
            item
            for item in research_worker.DEFAULT_GLOBAL_DISCOVERY_SEEDS
            if item.get("venue_or_source") == "Bybit"
        ]
        self.assertEqual(len(bybit_candidates), 1)
        candidate = bybit_candidates[0]
        self.assertEqual(candidate["recommended_next_action"], "adapter_spec")
        self.assertEqual(candidate["data_access_type"], "public_no_key")
        self.assertIn("spot", candidate["asset_or_event"].lower())
        self.assertIn("bybit-exchange.github.io", candidate["public_docs_url"])

    def test_bybit_linear_403_creates_spot_fallback_gap(self):
        gaps = llm_bridge._crypto_venue_health_gaps(
            [
                {
                    "venue": "bybit",
                    "route_id": "bybit_perp_public",
                    "asset": "BTCUSDT linear",
                    "status": "HTTP Error 403: Forbidden",
                    "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
                },
                {
                    "venue": "coinbase",
                    "route_id": "coinbase_spot_public",
                    "asset": "BTC-USD",
                    "status": "200",
                    "url": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
                },
            ]
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["fallback_route_id"], "bybit_spot_public")
        self.assertEqual(gaps[0]["paper_only_use"], "scanner_inputs_and_venue_health")
        self.assertIn("category=spot", " ".join(gaps[0]["fallback_endpoints"]))
