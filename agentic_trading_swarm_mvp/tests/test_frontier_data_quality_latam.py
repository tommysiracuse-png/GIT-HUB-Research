import unittest

from src.frontier_data_quality import normalize_bitso_order_book, normalize_buda_order_book


class FrontierDataQualityLatamTests(unittest.TestCase):
    def test_normalize_bitso_order_book_nested_payload(self):
        payload = {
            "payload": {
                "bids": [{"r": "100.0", "a": "0.50"}],
                "asks": [{"rate": "101.0", "amount": "0.25"}],
            }
        }
        book = normalize_bitso_order_book(payload)
        self.assertEqual(book["venue_name"], "BITSO")
        self.assertTrue(book["paper_only"])
        self.assertTrue(book["read_only"])
        self.assertEqual(book["best_bid"], 100.0)
        self.assertEqual(book["best_ask"], 101.0)
        self.assertEqual(book["book_state"], "ok")
        self.assertGreater(book["mid_price"], 0.0)
        self.assertGreater(book["spread_bps"], 0.0)

    def test_normalize_buda_order_book_nested_order_book(self):
        payload = {
            "order_book": {
                "bids": [["70000000", "0.010"]],
                "asks": [{"bookRate": "70500000", "volume": "0.020"}],
            }
        }
        book = normalize_buda_order_book(payload)
        self.assertEqual(book["venue_name"], "BUDA")
        self.assertTrue(book["paper_only"])
        self.assertTrue(book["read_only"])
        self.assertEqual(book["best_bid"], 70000000.0)
        self.assertEqual(book["best_ask"], 70500000.0)
        self.assertEqual(book["book_state"], "ok")
        self.assertEqual(book["level_count"], 2)
        self.assertGreater(book["mid_price"], 0.0)
        self.assertGreater(book["spread_bps"], 0.0)


if __name__ == "__main__":
    unittest.main()
