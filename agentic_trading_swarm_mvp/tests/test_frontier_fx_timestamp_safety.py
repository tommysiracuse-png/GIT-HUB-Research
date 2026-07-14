import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as adapter
import frontier_data_quality as data_quality


class _MockResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FrontierFxTimestampSafetyTests(unittest.TestCase):
    def test_adapter_fetch_json_includes_received_at(self) -> None:
        response = _MockResponse(b'{"symbol":"BTCUSDT"}')
        with mock.patch.object(adapter.urllib.request, "urlopen", return_value=response):
            result = adapter.fetch_json("https://example.test/api")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_status"], "reachable")
        self.assertIn("received_at", result)
        self.assertIn("T", result["received_at"])

    def test_default_registry_exposes_regional_fx_safety_filters(self) -> None:
        filters = adapter.DEFAULT_REGISTRY["filters"]
        self.assertIs(filters["regional_fx_normalization_enabled"], True)
        self.assertIs(filters["regional_fx_require_fresh_reference"], True)
        self.assertEqual(filters["regional_fx_max_age_seconds"], 21_600)
        self.assertEqual(filters["regional_fx_stale_confidence_haircut"], 0.35)

    def test_timestamp_to_iso_accepts_iso_strings(self) -> None:
        self.assertEqual(
            data_quality._timestamp_to_iso("2026-07-11T12:00:00Z"),
            "2026-07-11T12:00:00+00:00",
        )

    def test_extract_depth_normalizes_bitso_updated_at(self) -> None:
        depth = data_quality._extract_depth(
            "bitso_order_book",
            {
                "payload": {
                    "bids": [["100.0", "1.0"]],
                    "asks": [["101.0", "1.0"]],
                    "updated_at": "2026-07-11T12:00:00Z",
                }
            },
            "2026-07-11T12:01:00+00:00",
        )
        self.assertEqual(depth["book_timestamp"], "2026-07-11T12:00:00+00:00")
        self.assertEqual(depth["freshness_basis"], "exchange_timestamp")

    def test_extract_depth_keeps_received_at_when_no_exchange_timestamp(self) -> None:
        depth = data_quality._extract_depth(
            "bitso_order_book",
            {"payload": {"bids": [["100.0", "1.0"]], "asks": [["101.0", "1.0"]]}},
            "2026-07-11T12:01:00+00:00",
        )
        self.assertEqual(depth["book_timestamp"], "2026-07-11T12:01:00+00:00")
        self.assertEqual(depth["freshness_basis"], "response_received")


if __name__ == "__main__":
    unittest.main()
