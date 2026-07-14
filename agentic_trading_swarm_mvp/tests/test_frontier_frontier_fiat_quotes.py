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

    def test_new_frontier_quotes_are_marked_for_paper_only_review(self):
        row = {
            "quote": "UGX",
            "usd_normalized_last": 64250.0,
            "quote_normalization_source": "reference_fx:UGXUSD",
        }
        reviewed = adapter._apply_paper_only_review_policy(row)
        self.assertTrue(reviewed["local_quote_observe_only"])
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


if __name__ == "__main__":
    unittest.main()
