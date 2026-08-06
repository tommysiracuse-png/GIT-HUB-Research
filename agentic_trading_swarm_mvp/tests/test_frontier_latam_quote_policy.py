import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as adapter


class FrontierLatamQuotePolicyTests(unittest.TestCase):
    def test_finalize_keeps_latam_quote_rankable_when_normalized(self):
        row = {
            "symbol": "BTC-CLP",
            "base": "BTC",
            "quote": "CLP",
            "bid": 99.0,
            "ask": 101.0,
            "last": 100.0,
            "mark_price": None,
            "index_price": None,
            "usd_normalized_last": 0.105,
            "quote_normalization_source": "external_fx_reference",
            "local_quote_observe_only": False,
            "notes": [],
        }

        finalized = adapter._finalize_observation(row)

        self.assertFalse(finalized["local_quote_observe_only"])
        self.assertEqual(finalized["paper_only_review_scope"], "frontier_candidate_review")
        self.assertIn("usd_normalized_via_reference_fx", finalized["notes"])

    def test_finalize_marks_latam_quote_as_review_only_when_fx_is_missing(self):
        row = {
            "symbol": "ETH_ARS",
            "base": None,
            "quote": None,
            "bid": 9.0,
            "ask": 11.0,
            "last": 10.0,
            "mark_price": None,
            "index_price": None,
            "usd_normalized_last": None,
            "quote_normalization_source": None,
            "local_quote_observe_only": False,
            "notes": [],
        }

        finalized = adapter._finalize_observation(row)

        self.assertEqual(finalized["base"], "ETH")
        self.assertEqual(finalized["quote"], "ARS")
        self.assertTrue(finalized["local_quote_observe_only"])
        self.assertEqual(finalized["paper_only_review_scope"], "frontier_candidate_review")
        self.assertIn("review_only_pending_usd_normalization", finalized["notes"])

    def test_finalize_leaves_usd_like_quote_routable(self):
        row = {"symbol": "BTC-USDT", "bid": 99.0, "ask": 101.0, "last": 100.0, "notes": []}

        finalized = adapter._finalize_observation(row)

        self.assertFalse(finalized.get("local_quote_observe_only", False))
        self.assertIsNone(finalized.get("paper_only_review_scope"))


