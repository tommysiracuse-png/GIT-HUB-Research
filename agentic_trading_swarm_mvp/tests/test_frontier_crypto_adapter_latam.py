import unittest

from src.frontier_crypto_adapter import _normalize_latam_fiat_quote, classify_fiat_corridor


class FrontierCryptoAdapterLatamTests(unittest.TestCase):
    def test_normalize_latam_quote_aliases(self):
        result = _normalize_latam_fiat_quote("mx$", venue_name="Bitso")
        self.assertEqual(result["raw_quote"], "MX$")
        self.assertEqual(result["normalized_quote"], "MXN")
        self.assertTrue(result["alias_applied"])
        self.assertEqual(result["quote_region"], "mx")
        self.assertEqual(result["venue_region"], "mx")
        self.assertTrue(result["venue_quote_region_match"])

    def test_classify_latam_local_fiat_corridor(self):
        result = classify_fiat_corridor(
            "btc",
            "clp$",
            venue_name="BUDA",
            venue_notes="local fiat top-of-book shadow only",
        )
        self.assertEqual(result["corridor_base"], "BTC")
        self.assertEqual(result["corridor_quote"], "CLP")
        self.assertEqual(result["corridor_type"], "latam_local_fiat")
        self.assertEqual(result["corridor_region"], "cl")
        self.assertTrue(result["regional_quote_alias_applied"])
        self.assertGreaterEqual(result["corridor_confidence"], 0.8)

    def test_global_quote_path_unchanged(self):
        result = classify_fiat_corridor("eth", "usdt", venue_name="BITSO")
        self.assertEqual(result["corridor_type"], "global_usdt")
        self.assertEqual(result["corridor_quote"], "USDT")
        self.assertIsNone(result["corridor_region"])
        self.assertFalse(result["regional_quote_alias_applied"])


if __name__ == "__main__":
    unittest.main()
