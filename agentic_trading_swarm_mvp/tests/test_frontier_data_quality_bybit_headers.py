import unittest

from src.frontier_data_quality import paper_only_public_probe_headers


class TestFrontierDataQualityBybitHeaders(unittest.TestCase):
    def test_bybit_probe_headers_include_browser_fields(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        headers = paper_only_public_probe_headers(url)

        self.assertEqual(headers["Origin"], "https://www.bybit.com")
        self.assertEqual(headers["Referer"], "https://www.bybit.com/")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertIn("User-Agent", headers)

    def test_non_bybit_probe_headers_remain_generic(self):
        url = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"
        headers = paper_only_public_probe_headers(url)

        self.assertIn("User-Agent", headers)
        self.assertNotIn("Origin", headers)
        self.assertNotIn("Referer", headers)
        self.assertNotIn("X-Requested-With", headers)

    def test_extra_headers_override_defaults(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
        headers = paper_only_public_probe_headers(url, extra_headers={"Referer": "https://example.invalid/", "X-Test": "1"})

        self.assertEqual(headers["Referer"], "https://example.invalid/")
        self.assertEqual(headers["X-Test"], "1")


if __name__ == "__main__":
    unittest.main()
import unittest

from src.frontier_data_quality import paper_only_bybit_health_route_candidates


class BybitHealthRouteCandidatesTests(unittest.TestCase):
    def test_linear_canary_ticker_gets_host_and_orderbook_fallback_candidates(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"

        candidates = paper_only_bybit_health_route_candidates(url)

        self.assertEqual(
            candidates,
            [
                "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
                "https://api.bytick.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
                "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=1",
                "https://api.bytick.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=1",
                "https://api2.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=1",
            ],
        )

    def test_non_canary_symbol_only_gets_host_failover(self):
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=XRPUSDT"

        candidates = paper_only_bybit_health_route_candidates(url)

        self.assertEqual(
            candidates,
            [
                "https://api.bybit.com/v5/market/tickers?category=linear&symbol=XRPUSDT",
                "https://api.bytick.com/v5/market/tickers?category=linear&symbol=XRPUSDT",
            ],
        )

    def test_non_bybit_url_is_left_untouched(self):
        url = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"

        candidates = paper_only_bybit_health_route_candidates(url)

        self.assertEqual(
            candidates,
            [url],
        )


if __name__ == "__main__":
    unittest.main()
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_bridge import _crypto_venue_health_gaps
from research_worker import DEFAULT_GLOBAL_DISCOVERY_SEEDS


class BybitPublicHealthRepairTests(unittest.TestCase):
    def test_bybit_gap_uses_explicit_spot_symbol_probe(self) -> None:
        items = [
            {
                "venue": "Bybit",
                "status": "forbidden",
                "response_status": 403,
                "route_id": "bybit_perp_public",
                "asset": "BTCUSDT",
                "url": "https://api.bybit.com/v5/market/tickers?category=linear",
            }
        ]

        gaps = _crypto_venue_health_gaps(items)

        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["fallback_route_id"], "bybit_spot_public")
        self.assertEqual(
            gap["fallback_endpoints"],
            [
                "/v5/market/tickers?category=spot&symbol=BTCUSDT",
                "/v5/market/orderbook?category=spot&symbol=BTCUSDT",
            ],
        )
        self.assertEqual(gap["response_status"], 403)
        self.assertEqual(gap["adapter_fix"]["query"], {"category": "spot", "symbol": "BTCUSDT"})
        self.assertEqual(gap["adapter_fix"]["headers"]["Accept"], "application/json")
        self.assertEqual(gap["adapter_fix"]["headers"]["User-Agent"], "paper-research")
        self.assertEqual(gap["adapter_trace"], {"observed_url": items[0]["url"]})

    def test_existing_trace_is_preserved_for_bybit_gap(self) -> None:
        trace = {
            "observed_url": "https://api.bybit.com/v5/market/tickers?category=linear",
            "response_headers": {"server": "cloudflare"},
        }
        items = [
            {
                "venue": "Bybit",
                "status": "HTTP 403",
                "route_id": "bybit_linear_perp_public",
                "adapter_trace": trace,
            },
            {"venue": "OKX", "status": "HTTP 403", "route_id": "okx_public"},
        ]

        gaps = _crypto_venue_health_gaps(items)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["adapter_trace"], trace)

    def test_research_worker_seed_matches_bybit_repair_shape(self) -> None:
        bybit_seed = next(seed for seed in DEFAULT_GLOBAL_DISCOVERY_SEEDS if seed.get("venue_or_source") == "Bybit")
        self.assertEqual(bybit_seed["adapter_route_id"], "bybit_perp_public")
        self.assertEqual(bybit_seed["adapter_request_hint"]["query"], {"category": "spot", "symbol": "BTCUSDT"})
        self.assertEqual(bybit_seed["adapter_request_hint"]["headers"]["Accept"], "application/json")
        self.assertEqual(bybit_seed["adapter_request_hint"]["headers"]["User-Agent"], "paper-research")


if __name__ == "__main__":
    unittest.main()
import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from llm_bridge import _crypto_venue_health_gaps
from research_worker import DEFAULT_GLOBAL_DISCOVERY_SEEDS


class BybitPublicAdapterHintsTests(unittest.TestCase):
    def test_bybit_linear_403_gap_preserves_route_and_emits_safe_request_hints(self):
        items = [
            {
                "venue": "bybit",
                "route_id": "bybit_perp_public",
                "asset": "BTCUSDT linear",
                "status": "HTTP Error: Forbidden",
                "response_status": 403,
                "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
                "adapter_trace": {"request_path": "/v5/market/tickers", "query": {"category": "linear"}},
            }
        ]

        gaps = _crypto_venue_health_gaps(items)

        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap["route_id"], "bybit_perp_public")
        self.assertEqual(gap["fallback_route_id"], "bybit_spot_public")
        self.assertEqual(gap["response_status"], 403)
        self.assertEqual(gap["adapter_trace"]["request_path"], "/v5/market/tickers")
        self.assertEqual(gap["adapter_fix"]["method"], "GET")
        self.assertEqual(gap["adapter_fix"]["path"], "/v5/market/tickers")
        self.assertEqual(gap["adapter_fix"]["query"]["category"], "spot")
        self.assertEqual(gap["adapter_fix"]["headers"]["Accept"], "application/json")
        self.assertEqual(gap["adapter_fix"]["headers"]["User-Agent"], "paper-research")
        self.assertIn("result.list[0].lastPrice", gap["adapter_fix"]["response_fields"])

    def test_bybit_spot_seed_carries_public_request_shape_for_paper_adapter_specs(self):
        bybit_seed = next(
            item for item in DEFAULT_GLOBAL_DISCOVERY_SEEDS if str(item.get("venue_or_source") or "").lower() == "bybit"
        )

        self.assertEqual(bybit_seed["adapter_route_id"], "bybit_perp_public")
        self.assertEqual(bybit_seed["adapter_request_hint"]["method"], "GET")
        self.assertEqual(bybit_seed["adapter_request_hint"]["path"], "/v5/market/tickers")
        self.assertEqual(bybit_seed["adapter_request_hint"]["query"]["category"], "spot")
        self.assertEqual(bybit_seed["adapter_request_hint"]["headers"]["Accept"], "application/json")
        self.assertEqual(bybit_seed["adapter_request_hint"]["headers"]["User-Agent"], "paper-research")
        self.assertIn(
            "result.list[0].bid1Price",
            bybit_seed["adapter_request_hint"]["response_fields"],
        )

    def test_non_linear_or_non_bybit_rows_do_not_create_gap_hints(self):
        items = [
            {"venue": "okx", "status": "403", "route_id": "okx_perp_public", "url": "https://okx.example/linear"},
            {"venue": "bybit", "status": "403", "route_id": "bybit_spot_public", "url": "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"},
        ]
        self.assertEqual(_crypto_venue_health_gaps(items), [])


if __name__ == "__main__":
    unittest.main()
import importlib
import unittest


frontier_data_quality = importlib.import_module("src.frontier_data_quality")


class BybitPublicHeaderProfileTests(unittest.TestCase):
    def test_bybit_browser_headers_extend_default_public_headers(self):
        headers = frontier_data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS
        default_headers = frontier_data_quality._DEFAULT_PUBLIC_HEADERS

        self.assertEqual(headers["User-Agent"], default_headers["User-Agent"])
        self.assertEqual(headers["Accept"], default_headers["Accept"])
        self.assertEqual(headers["Accept-Encoding"], default_headers["Accept-Encoding"])
        self.assertEqual(headers["Origin"], "https://www.bybit.com")
        self.assertEqual(headers["Referer"], "https://www.bybit.com/")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_bybit_header_profile_keeps_browser_client_hints(self):
        headers = frontier_data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS

        self.assertIn("Chromium", headers["Sec-CH-UA"])
        self.assertEqual(headers["Sec-CH-UA-Mobile"], "?0")
        self.assertEqual(headers["Sec-CH-UA-Platform"], '"Windows"')
        self.assertEqual(headers["Priority"], "u=1, i")

    def test_bybit_failover_host_mapping_remains_available(self):
        self.assertEqual(frontier_data_quality._BYBIT_PUBLIC_FAILOVER_HOSTS.get("api.bybit.com"), "api.bytick.com")


if __name__ == "__main__":
    unittest.main()
import unittest

from src import frontier_data_quality as data_quality


class TestBybitPublicHeaders(unittest.TestCase):
    def test_default_public_user_agent_is_browser_like(self):
        headers = data_quality._DEFAULT_PUBLIC_HEADERS
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("Chrome/", headers["User-Agent"])
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_bybit_headers_include_read_only_browser_fields(self):
        headers = data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS
        self.assertEqual(headers["Origin"], "https://www.bybit.com")
        self.assertEqual(headers["Referer"], "https://www.bybit.com/")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertEqual(headers["DNT"], "1")
        self.assertEqual(headers["Sec-Fetch-Dest"], "empty")
        self.assertEqual(headers["Sec-Fetch-Mode"], "cors")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-site")
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")

    def test_bybit_headers_preserve_default_accept_and_user_agent(self):
        self.assertEqual(
            data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS["Accept"],
            data_quality._DEFAULT_PUBLIC_HEADERS["Accept"],
        )
        self.assertEqual(
            data_quality._BYBIT_READ_ONLY_BROWSER_HEADERS["User-Agent"],
            data_quality._DEFAULT_PUBLIC_HEADERS["User-Agent"],
        )
