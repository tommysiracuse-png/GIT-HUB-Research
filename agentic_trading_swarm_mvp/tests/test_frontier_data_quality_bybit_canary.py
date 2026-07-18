import unittest

from src.frontier_data_quality import (
    _build_public_request,
    _is_bybit_linear_perp_canary_url,
    _route_access_report,
)


class BybitLinearPerpCanaryTests(unittest.TestCase):
    def test_canary_url_detection_is_limited_to_btc_and_eth_linear_perps(self):
        self.assertTrue(
            _is_bybit_linear_perp_canary_url(
                "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
            )
        )
        self.assertTrue(
            _is_bybit_linear_perp_canary_url(
                "https://api.bytick.com/v5/market/tickers?category=linear&symbol=ethusdt"
            )
        )
        self.assertFalse(
            _is_bybit_linear_perp_canary_url(
                "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"
            )
        )
        self.assertFalse(
            _is_bybit_linear_perp_canary_url(
                "https://api.bybit.com/v5/market/tickers?category=linear&symbol=SOLUSDT"
            )
        )

    def test_build_public_request_uses_browser_headers_for_canary_urls(self):
        request = _build_public_request(
            "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get("origin"), "https://www.bybit.com")
        self.assertEqual(headers.get("referer"), "https://www.bybit.com/")
        self.assertEqual(headers.get("accept-language"), "en-US,en;q=0.9")
        self.assertIn("user-agent", headers)

    def test_build_public_request_keeps_non_canary_requests_generic(self):
        request = _build_public_request(
            "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("origin", headers)
        self.assertNotIn("referer", headers)

    def test_route_access_report_includes_canary_route_health(self):
        requested_url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        fallback_url = "https://api.bytick.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        report = _route_access_report(
            requested_url=requested_url,
            effective_url=fallback_url,
            primary_access={
                "endpoint_access": "restricted",
                "blocked_http_status": "403",
                "blocked_reason": "http_403",
            },
            fallback_url=fallback_url,
            fallback_access={
                "endpoint_access": "reachable",
                "blocked_http_status": None,
                "blocked_reason": None,
            },
            fallback_attempted=True,
        )
        self.assertEqual(report["resolution"], "fallback")
        self.assertEqual(report["route_health"]["state"], "reachable_via_fallback")
        self.assertTrue(report["route_health"]["bybit_linear_perp_canary"])
        self.assertTrue(report["route_health"]["paper_only"])
        self.assertTrue(report["route_health"]["read_only"])


if __name__ == "__main__":
    unittest.main()
