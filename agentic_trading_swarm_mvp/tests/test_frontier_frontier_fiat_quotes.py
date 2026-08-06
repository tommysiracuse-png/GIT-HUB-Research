import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as adapter


class FrontierFiatQuoteSupportTests(unittest.TestCase):
    def test_split_symbol_supports_new_frontier_quotes(self):
        self.assertEqual(
            adapter._split_symbol("BTCUGX", adapter.QUOTE_ASSETS),
            ("BTC", "UGX"),
        )
        self.assertEqual(
            adapter._split_symbol("ETH-TZS", adapter.QUOTE_ASSETS),
            ("ETH", "TZS"),
        )

    def test_default_registry_filters_include_new_frontier_quotes(self):
        quote_assets = set(adapter.DEFAULT_REGISTRY["filters"]["quote_assets"])
        self.assertIn("UGX", quote_assets)
        self.assertIn("TZS", quote_assets)

    def test_new_frontier_quotes_are_rankable_after_reference_fx_normalization(self):
        row = {
            "quote": "UGX",
            "usd_normalized_last": 64250.0,
            "quote_normalization_source": "reference_fx:UGXUSD",
        }
        reviewed = adapter._apply_paper_only_review_policy(row)
        self.assertFalse(reviewed["local_quote_observe_only"])
        self.assertEqual(reviewed["paper_only_review_scope"], "frontier_candidate_review")
        self.assertIn("usd_normalized_via_reference_fx", reviewed["notes"])

    def test_existing_non_review_quotes_keep_current_policy(self):
        row = {
            "quote": "ZAR",
            "usd_normalized_last": 64250.0,
            "quote_normalization_source": "reference_fx:ZARUSD",
            "local_quote_observe_only": False,
            "paper_only_review_scope": None,
            "notes": [],
        }
        reviewed = adapter._apply_paper_only_review_policy(row)
        self.assertFalse(reviewed["local_quote_observe_only"])
        self.assertIsNone(reviewed["paper_only_review_scope"])
        self.assertEqual(reviewed["notes"], [])

    def test_external_fx_normalization_admits_new_frontier_quote_into_reference_ranking(self):
        normalized = adapter._normalize_regional_quotes(
            [
                {
                    "venue": "QUIDAX",
                    "market_type": "spot",
                    "symbol": "BTCUGX",
                    "instrument_id": "QUIDAX:BTCUGX",
                    "base": "BTC",
                    "quote": "UGX",
                    "comparison_key": "BTC",
                    "last": 236_800_000.0,
                    "quote_volume_24h": 23_680_000_000.0,
                    "data_status": "reachable",
                    "source_url": "https://example.test/quidax",
                    "route_id": "quidax_spot_public",
                    "last_checked_at": "2026-08-06T12:00:00+00:00",
                    "notes": [],
                },
                {
                    "venue": "COINBASE",
                    "market_type": "spot",
                    "symbol": "BTC-USD",
                    "instrument_id": "COINBASE:BTC-USD",
                    "base": "BTC",
                    "quote": "USD",
                    "comparison_key": "BTC",
                    "last": 64_000.0,
                    "quote_volume_24h": 10_000_000.0,
                    "data_status": "reachable",
                    "source_url": "https://example.test/coinbase",
                    "route_id": "coinbase_spot_public",
                    "last_checked_at": "2026-08-06T12:00:00+00:00",
                    "notes": [],
                },
            ],
            fx_references={
                "UGX": {
                    "rate": 3700.0,
                    "provider": "trusted_fx",
                    "age_seconds": 15.0,
                    "source_url": "https://example.test/fx",
                }
            },
        )
        quidax = next(row for row in normalized if row["venue"] == "QUIDAX")
        refs = adapter._reference_prices(normalized, {})

        self.assertEqual("external_fx_reference", quidax["quote_normalization_status"])
        self.assertFalse(quidax["local_quote_observe_only"])
        self.assertAlmostEqual(64_000.0, quidax["usd_normalized_last"])
        self.assertAlmostEqual(1.0 / 3700.0, quidax["quote_to_usd_multiplier"], places=12)
        self.assertIn("BTC", refs)
        self.assertAlmostEqual(64_000.0, refs["BTC"])


if __name__ == "__main__":
    unittest.main()
