import unittest
import urllib.error
from unittest import mock

from src import frontier_data_quality as fdq


class FrontierDataQualityRouteAccessTests(unittest.TestCase):
    def test_bybit_public_failover_url_for_market_endpoint(self):
        url = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT"
        expected = "https://api.bytick.com/v5/market/orderbook?category=linear&symbol=BTCUSDT"
        self.assertEqual(fdq._bybit_public_failover_url(url), expected)

    def test_fetch_json_records_primary_and_fallback_route_access(self):
        primary_url = "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT"
        fallback_url = "https://api.bytick.com/v5/market/orderbook?category=linear&symbol=BTCUSDT"
        primary_error = urllib.error.HTTPError(primary_url, 403, "forbidden", hdrs=None, fp=None)

        def fake_open_json_request(request, timeout, started):
            if request.full_url == primary_url:
                raise primary_error
            self.assertEqual(request.full_url, fallback_url)
            self.assertEqual(request.headers.get("Origin"), "https://www.bybit.com")
            return {
                "ok": True,
                "status": "reachable",
                "http_status": "200",
                "latency_ms": 1.0,
                "received_at": "2026-07-18T00:00:00+00:00",
                "payload": {"result": {"b": [], "a": []}},
                "endpoint_access": "reachable",
                "blocked_http_status": None,
                "blocked_reason": None,
            }

        with mock.patch("src.frontier_data_quality._open_json_request", side_effect=fake_open_json_request):
            result = fdq._fetch_json(primary_url, timeout=5)

        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_status"], "fallback_success")
        self.assertEqual(result["primary_http_status"], "403")
        self.assertEqual(result["requested_url"], primary_url)
        self.assertEqual(result["source_url"], fallback_url)
        self.assertEqual(result["effective_url"], fallback_url)
        self.assertEqual(result["fallback_url"], fallback_url)

        route_access = result["route_access"]
        self.assertEqual(route_access["requested_url"], primary_url)
        self.assertEqual(route_access["effective_url"], fallback_url)
        self.assertEqual(route_access["fallback_candidate_url"], fallback_url)
        self.assertTrue(route_access["fallback_attempted"])
        self.assertEqual(route_access["resolution"], "fallback")

        self.assertEqual(route_access["primary"]["url"], primary_url)
        self.assertEqual(route_access["primary"]["endpoint_access"], "restricted")
        self.assertEqual(route_access["primary"]["blocked_http_status"], "403")
        self.assertEqual(route_access["primary"]["blocked_reason"], "http_403")

        self.assertEqual(route_access["fallback"]["url"], fallback_url)
        self.assertEqual(route_access["fallback"]["endpoint_access"], "reachable")


if __name__ == "__main__":
    unittest.main()
