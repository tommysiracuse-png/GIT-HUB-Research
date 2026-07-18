import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import frontier_data_quality as data_quality


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BybitPublicFailoverTests(unittest.TestCase):
    def test_fetch_json_retries_bybit_public_403_on_secondary_hostname(self):
        primary_url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        attempted_requests = []

        def fake_urlopen(request, timeout=0):
            attempted_requests.append(request)
            if len(attempted_requests) == 1:
                raise urllib.error.HTTPError(primary_url, 403, "Forbidden", hdrs=None, fp=None)
            return _FakeResponse({"result": {"list": [{"symbol": "BTCUSDT"}]}})

        with mock.patch.object(data_quality.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = data_quality._fetch_json(primary_url, timeout=2)

        self.assertTrue(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_status"], "fallback_success")
        self.assertEqual(result["primary_http_status"], "403")
        self.assertEqual(
            result["source_url"],
            "https://api.bytick.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
        )
        self.assertEqual(len(attempted_requests), 2)
        self.assertEqual(
            data_quality.urllib.parse.urlsplit(attempted_requests[1].full_url).netloc,
            "api.bytick.com",
        )
        fallback_headers = {
            key.lower(): value for key, value in attempted_requests[1].header_items()
        }
        self.assertEqual(fallback_headers.get("origin"), "https://www.bybit.com")
        self.assertEqual(fallback_headers.get("referer"), "https://www.bybit.com/")

    def test_fetch_json_marks_blocked_403_when_secondary_hostname_is_also_forbidden(self):
        primary_url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
        attempted_urls = []

        def fake_urlopen(request, timeout=0):
            attempted_urls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with mock.patch.object(data_quality.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = data_quality._fetch_json(primary_url, timeout=2)

        self.assertFalse(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_status"], "blocked_403")
        self.assertEqual(result["endpoint_access"], "restricted")
        self.assertEqual(result["blocked_http_status"], "403")
        self.assertEqual(len(attempted_urls), 2)
        self.assertEqual(
            attempted_urls[1],
            "https://api.bytick.com/v5/market/tickers?category=linear&symbol=ETHUSDT",
        )

    def test_fetch_json_does_not_retry_non_bybit_403(self):
        url = "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"
        attempted_urls = []

        def fake_urlopen(request, timeout=0):
            attempted_urls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with mock.patch.object(data_quality.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = data_quality._fetch_json(url, timeout=2)

        self.assertFalse(result["ok"])
        self.assertFalse(result["fallback_used"])
        self.assertIsNone(result["fallback_status"])
        self.assertEqual(result["source_url"], url)
        self.assertEqual(attempted_urls, [url])


if __name__ == "__main__":
    unittest.main()
