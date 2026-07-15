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
