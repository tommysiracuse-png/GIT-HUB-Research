import unittest

from src.frontier_data_quality import (
    _bybit_ticker_last_price,
    _response_access_metadata,
    _route_health_state,
)


class BybitPerpFallbackTests(unittest.TestCase):
    def setUp(self):
        self.bybit_url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        self.bytick_url = "https://api.bytick.com/v5/market/tickers?category=linear&symbol=BTCUSDT"

    def test_extracts_last_price_from_v5_ticker_payload(self):
        payload = {
            "retCode": 0,
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "65432.1",
                    }
                ],
            },
        }
        self.assertEqual(_bybit_ticker_last_price(payload), 65432.1)

    def test_response_access_requires_numeric_last_price_for_bybit_canary(self):
        metadata = _response_access_metadata(
            {
                "url": self.bytick_url,
                "endpoint_access": "reachable",
                "payload": {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [{"symbol": "BTCUSDT", "lastPrice": ""}],
                    },
                },
            }
        )
        self.assertEqual(metadata["endpoint_access"], "unavailable")
        self.assertEqual(metadata["blocked_reason"], "missing_last_price")

    def test_route_health_does_not_recover_via_fallback_without_last_price(self):
        primary_access = {
            "url": self.bybit_url,
            "endpoint_access": "restricted",
            "blocked_http_status": "403",
            "blocked_reason": "http_403",
        }
        fallback_access = {
            "url": self.bytick_url,
            "endpoint_access": "reachable",
            "payload": {
                "retCode": 0,
                "result": {"category": "linear", "list": [{"symbol": "BTCUSDT"}]},
            },
        }
        self.assertEqual(
            _route_health_state(primary_access, fallback_access, fallback_attempted=True),
            "restricted_with_failed_fallback",
        )

    def test_route_health_recovers_via_fallback_with_last_price(self):
        primary_access = {"url": self.bybit_url, "endpoint_access": "restricted"}
        fallback_access = {
            "url": self.bytick_url,
            "endpoint_access": "reachable",
            "payload": {
                "retCode": 0,
                "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "lastPrice": "65000"}]},
            },
        }
        self.assertEqual(_route_health_state(primary_access, fallback_access, fallback_attempted=True), "reachable_via_fallback")


if __name__ == "__main__":
    unittest.main()
