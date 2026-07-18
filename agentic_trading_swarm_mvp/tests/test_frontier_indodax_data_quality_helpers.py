import unittest

from src.frontier_data_quality import normalize_indodax_order_book


class TestIndodaxDataQualityHelpers(unittest.TestCase):
    def test_normalize_indodax_order_book_from_buy_sell_lists(self):
        payload = {
            "buy": [["100", "2.0"], ["99", "1.5"]],
            "sell": [["101", "1.0"], ["102", "3.0"]],
        }

        normalized = normalize_indodax_order_book(payload)

        self.assertEqual(normalized["book_state"], "ok")
        self.assertEqual(normalized["best_bid"], 100.0)
        self.assertEqual(normalized["best_ask"], 101.0)
        self.assertEqual(normalized["level_count"], 4)
        self.assertAlmostEqual(normalized["spread_bps"], 99.50248756218906)

    def test_normalize_indodax_order_book_accepts_dict_levels(self):
        payload = {
            "bids": [{"price": "200", "amount": "1.25"}],
            "asks": [{"rate": "201", "qty": "0.75"}],
        }

        normalized = normalize_indodax_order_book(payload)

        self.assertEqual(normalized["book_state"], "ok")
        self.assertEqual(normalized["best_bid"], 200.0)
        self.assertEqual(normalized["best_ask"], 201.0)
        self.assertTrue(normalized["paper_only"])
        self.assertTrue(normalized["read_only"])

    def test_normalize_indodax_order_book_detects_one_sided_and_crossed(self):
        one_sided = normalize_indodax_order_book({"buy": [["100", "1"]]})
        crossed = normalize_indodax_order_book(
            {
                "buy": [["101", "1"]],
                "sell": [["100", "1"]],
            }
        )

        self.assertEqual(one_sided["book_state"], "one_sided_book")
        self.assertEqual(crossed["book_state"], "crossed_book")

    def test_normalize_indodax_order_book_ignores_invalid_levels(self):
        payload = {"buy": [["0", "1"], ["100", "0"], ["99", "2"]], "sell": [["101", "1"]]}
        normalized = normalize_indodax_order_book(payload)
        self.assertEqual(normalized["best_bid"], 99.0)
        self.assertEqual(normalized["best_ask"], 101.0)
