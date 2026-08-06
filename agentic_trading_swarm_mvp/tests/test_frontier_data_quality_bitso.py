import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frontier_data_quality import _timestamp_to_iso


class BitsoTimestampSafetyTests(unittest.TestCase):
    def test_rejects_nonfinite_numeric_timestamps(self):
        self.assertIsNone(_timestamp_to_iso(float("nan")))
        self.assertIsNone(_timestamp_to_iso(float("inf")))
        self.assertIsNone(_timestamp_to_iso(float("-inf")))

    def test_rejects_invalid_string_payloads(self):
        self.assertIsNone(_timestamp_to_iso("not-a-timestamp"))
        self.assertIsNone(_timestamp_to_iso(object()))

    def test_preserves_iso_payloads(self):
        self.assertEqual(
            _timestamp_to_iso("2024-06-01T12:34:56Z"),
            "2024-06-01T12:34:56+00:00",
        )

    def test_explicit_microseconds_are_supported(self):
        self.assertEqual(
            _timestamp_to_iso(1_700_000_000_000_000, unit="microseconds"),
            "2023-11-14T22:13:20+00:00",
        )

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_data_quality as fdq


class BitsoDepthNormalizationTests(unittest.TestCase):
    def test_format_symbol_normalizes_bitso_mxn_variants(self):
        self.assertEqual(fdq._format_symbol("BITSO", "BTC/MXN"), "btc_mxn")
        self.assertEqual(fdq._format_symbol("BITSO", "BTC-MXN"), "btc_mxn")
        self.assertEqual(fdq._format_symbol("BITSO", "BTCMXN"), "btc_mxn")
        self.assertEqual(fdq._format_symbol("BITSO", "btc_mxn"), "btc_mxn")

    def test_build_depth_url_uses_normalized_bitso_book_symbol(self):
        observation = {
            "venue": "BITSO",
            "symbol": "BTC/MXN",
        }
        depth_config = {
            "url_template": "https://api.bitso.com/v3/order_book/?book={symbol}&aggregate=false",
            "max_levels": 20,
        }

        url = fdq._build_depth_url(observation, depth_config, levels=50)

        self.assertEqual(
            url,
            "https://api.bitso.com/v3/order_book/?book=btc_mxn&aggregate=false",
        )

    def test_build_depth_url_uses_normalized_compact_bitso_book_symbol(self):
        observation = {
            "venue": "BITSO",
            "symbol": "BTCUSDT",
        }
        depth_config = {
            "url_template": "https://api.bitso.com/v3/order_book/?book={symbol}&aggregate=false",
            "max_levels": 20,
        }

        url = fdq._build_depth_url(observation, depth_config, levels=50)

        self.assertEqual(
            url,
            "https://api.bitso.com/v3/order_book/?book=btc_usdt&aggregate=false",
        )

    def test_extract_depth_reads_bitso_exchange_timestamp(self):
        payload = {
            "payload": {
                "updated_at": "2026-07-15T00:00:00+00:00",
                "bids": [["100", "1.5"]],
                "asks": [["101", "2.0"]],
            }
        }

        extracted = fdq._extract_depth("bitso_order_book", payload, "2026-07-15T00:00:05+00:00")

        self.assertEqual(extracted["book_timestamp"], "2026-07-15T00:00:00+00:00")
        self.assertEqual(extracted["freshness_basis"], "exchange_timestamp")
        self.assertEqual(extracted["bids"], [["100", "1.5"]])
        self.assertEqual(extracted["asks"], [["101", "2.0"]])


if __name__ == "__main__":
    unittest.main()
