import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from frontier_crypto_adapter import DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS
from frontier_data_quality import _fetch_json


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


class FrontierPublicRouteMetadataTests(unittest.TestCase):
    def test_fetch_json_marks_reachable_endpoints(self):
        payload = {"result": {"bids": [], "asks": []}}
        with mock.patch(
            "frontier_data_quality.urllib.request.urlopen",
            return_value=_FakeResponse(payload, status=200),
        ):
            result = _fetch_json("https://example.test/orderbook", timeout=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "reachable")
        self.assertEqual(result["endpoint_access"], "reachable")
        self.assertIsNone(result["blocked_http_status"])
        self.assertIsNone(result["blocked_reason"])
        self.assertEqual(result["payload"], payload)

    def test_fetch_json_marks_blocked_http_routes_as_restricted(self):
        error = urllib.error.HTTPError(
            url="https://example.test/bybit",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        with mock.patch(
            "frontier_data_quality.urllib.request.urlopen",
            side_effect=error,
        ):
            result = _fetch_json("https://example.test/bybit", timeout=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["endpoint_access"], "restricted")
        self.assertEqual(result["blocked_http_status"], "403")
        self.assertEqual(result["blocked_reason"], "http_403")
        self.assertIsNone(result["payload"])

    def test_bybit_short_route_requirements_are_explicit(self):
        bybit = DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS["BYBIT"]
        bybit_spot = DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS["BYBIT_SPOT"]

        for route in (bybit, bybit_spot):
            self.assertTrue(route["supports_spot_short"])
            self.assertTrue(route["requires_margin_permission"])
            self.assertTrue(route["requires_borrow_check"])
            self.assertEqual(route["margin_mode_hint"], "unified_margin")
            self.assertEqual(route["api_route_hint"], "spot_margin")
            self.assertGreater(route["fee_bps_hint"], 0.0)

    def test_bybit_linear_route_requirements_remain_non_spot(self):
        self.assertEqual(
            DEFAULT_PAPER_ONLY_CONDITIONAL_SHORT_ROUTE_REQUIREMENTS["BYBIT_LINEAR"],
            {},
        )


if __name__ == "__main__":
    unittest.main()
