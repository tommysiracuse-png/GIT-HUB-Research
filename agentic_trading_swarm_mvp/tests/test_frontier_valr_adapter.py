import json
import pathlib
import unittest

from src.frontier_crypto_adapter import (
    _paper_only_valr_normalize_public_order_book,
    paper_only_valr_market_catalog,
    paper_only_valr_normalize_symbol,
    paper_only_valr_observation_from_public_payloads,
)
from src.frontier_data_quality import paper_only_valr_public_request_plan


FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ValrPaperAdapterTests(unittest.TestCase):
    def test_normalize_symbol_accepts_supported_zar_pairs(self):
        self.assertEqual(paper_only_valr_normalize_symbol("BTC/ZAR"), "BTCZAR")
        self.assertEqual(paper_only_valr_normalize_symbol("eth-zar"), "ETHZAR")
        self.assertEqual(paper_only_valr_normalize_symbol("USDT_ZAR"), "USDTZAR")
        self.assertIsNone(paper_only_valr_normalize_symbol("XRP/ZAR"))

    def test_market_catalog_exposes_read_only_endpoints(self):
        catalog = paper_only_valr_market_catalog()
        self.assertEqual([item["venue_symbol"] for item in catalog], ["BTCZAR", "ETHZAR", "USDTZAR"])
        self.assertTrue(all(item["paper_only"] for item in catalog))
        self.assertEqual(catalog[0]["endpoints"]["ticker"], "https://api.valr.com/v1/public/BTCZAR/marketsummary")
        self.assertEqual(catalog[0]["endpoints"]["top_of_book"], "https://api.valr.com/v1/public/BTCZAR/orderbook")
        self.assertEqual(
            catalog[0]["endpoints"]["recent_trades"],
            "https://api.valr.com/v1/public/BTCZAR/trades?limit=50",
        )

    def test_observation_builds_from_public_payloads(self):
        observation = paper_only_valr_observation_from_public_payloads(
            "BTC/ZAR",
            ticker_payload={
                "lastTradedPrice": "1234500.0",
                "quoteVolume": "25000000.0",
                "lastTradedTimestamp": "2026-07-21T23:45:30Z",
            },
            orderbook_payload={
                "bids": [{"price": "1234400.0", "quantity": "0.75"}],
                "asks": [{"price": "1234600.0", "quantity": "0.50"}],
            },
            trades_payload=[
                {
                    "price": "1234550.0",
                    "quantity": "0.10",
                    "tradedAt": "2026-07-21T23:45:00Z",
                }
            ],
            as_of="2026-07-21T23:46:00Z",
        )

        self.assertEqual(observation["venue"], "VALR")
        self.assertEqual(observation["symbol"], "BTC/ZAR")
        self.assertEqual(observation["venue_symbol"], "BTCZAR")
        self.assertEqual(observation["last_price"], 1234500.0)
        self.assertEqual(observation["best_bid"], 1234400.0)
        self.assertEqual(observation["best_ask"], 1234600.0)
        self.assertEqual(observation["mid_price"], 1234500.0)
        self.assertAlmostEqual(observation["spread_bps"], 1.6200891049007697)
        self.assertEqual(observation["bid_size"], 0.75)
        self.assertEqual(observation["ask_size"], 0.5)
        self.assertEqual(observation["recent_trade_count"], 1)
        self.assertEqual(observation["recent_trade_price"], 1234550.0)
        self.assertEqual(observation["recent_trade_quantity"], 0.10)
        self.assertEqual(observation["recent_trade_timestamp"], "2026-07-21T23:45:00+00:00")
        self.assertEqual(observation["last_trade_timestamp"], "2026-07-21T23:45:30+00:00")
        self.assertEqual(observation["observed_at"], "2026-07-21T23:46:00+00:00")
        self.assertEqual(observation["instrument_metadata"]["market_type"], "spot")
        self.assertEqual(observation["venue_constraints"]["price_precision"], 0)
        self.assertEqual(observation["venue_constraints"]["quantity_precision"], 2)
        self.assertEqual(observation["instrument_metadata"]["venue_constraints"]["constraint_source"], "observed_public_payload")
        self.assertEqual(observation["shallow_order_book"]["bids"], [[1234400.0, 0.75]])
        self.assertEqual(observation["market_data_origin"], "native_public_spot")

    def test_public_order_book_normalization_accepts_actual_uppercase_shape(self):
        cases = (
            ("valr_orderbook_btcusdt.json", 64375.0, 64426.0),
            ("valr_orderbook_solusdc.json", 72.79, 72.89),
        )

        for fixture_name, expected_bid, expected_ask in cases:
            with self.subTest(fixture=fixture_name):
                normalized = _paper_only_valr_normalize_public_order_book(_load_fixture(fixture_name), max_levels=3)
                self.assertEqual(expected_bid, normalized["best_bid"])
                self.assertEqual(expected_ask, normalized["best_ask"])
                self.assertEqual(expected_bid, normalized["bids"][0][0])
                self.assertEqual(expected_ask, normalized["asks"][0][0])
                self.assertGreater(normalized["spread_bps"], 0.0)
                self.assertEqual([], normalized["anomaly_flags"])


class ValrPaperDataQualityTests(unittest.TestCase):
    def test_public_request_plan_is_read_only_and_filtered(self):
        plan = paper_only_valr_public_request_plan(symbols=["btczar", "ETH/ZAR", "XRP/ZAR"], trade_limit=25)

        self.assertEqual(plan["venue"], "VALR")
        self.assertTrue(plan["paper_only"])
        self.assertEqual(plan["symbols"], ["BTC/ZAR", "ETH/ZAR"])
        self.assertEqual(len(plan["requests"]), 2)
        self.assertEqual(
            plan["requests"][0]["endpoints"],
            {
                "ticker": "https://api.valr.com/v1/public/BTCZAR/marketsummary",
                "top_of_book": "https://api.valr.com/v1/public/BTCZAR/orderbook",
                "recent_trades": "https://api.valr.com/v1/public/BTCZAR/trades?limit=25",
            },
        )
        self.assertEqual(
            plan["health_urls"],
            [
                "https://api.valr.com/v1/public/BTCZAR/marketsummary",
                "https://api.valr.com/v1/public/BTCZAR/orderbook",
                "https://api.valr.com/v1/public/BTCZAR/trades?limit=25",
                "https://api.valr.com/v1/public/ETHZAR/marketsummary",
                "https://api.valr.com/v1/public/ETHZAR/orderbook",
                "https://api.valr.com/v1/public/ETHZAR/trades?limit=25",
            ],
        )


if __name__ == "__main__":
    unittest.main()
