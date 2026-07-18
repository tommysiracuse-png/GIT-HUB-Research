import unittest

from src.frontier_crypto_adapter import (
    classify_fiat_corridor,
    paper_only_indodax_market_health,
    paper_only_indodax_symbol_discovery,
)


class TestPaperOnlyIndodaxAdapterHelpers(unittest.TestCase):
    def test_indodax_symbol_discovery_filters_to_idr_and_sorts_by_turnover(self):
        payload = {
            "tickers": {
                "btc_idr": {
                    "buy": "1700000000",
                    "sell": "1702000000",
                    "last": "1701000000",
                    "vol_idr": "550000000000",
                },
                "eth_idr": {
                    "buy": "55000000",
                    "sell": "55100000",
                    "last": "55050000",
                    "vol_idr": "210000000000",
                },
                "sol_usdt": {
                    "buy": "155",
                    "sell": "156",
                    "last": "155.5",
                    "vol_usdt": "999999999",
                },
            }
        }

        discovered = paper_only_indodax_symbol_discovery(payload, max_markets=10)

        self.assertEqual([item["symbol"] for item in discovered], ["BTC_IDR", "ETH_IDR"])
        self.assertTrue(all(item["quote_asset"] == "IDR" for item in discovered))
        self.assertTrue(all(item["paper_only"] for item in discovered))
        self.assertGreater(discovered[0]["quote_turnover"], discovered[1]["quote_turnover"])

    def test_indodax_symbol_discovery_falls_back_to_last_when_bid_ask_missing(self):
        payload = {
            "tickers": {
                "ada_idr": {
                    "last": "8250",
                    "vol_idr": "50000000",
                }
            }
        }

        discovered = paper_only_indodax_symbol_discovery(payload, max_markets=5)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["best_bid"], 8250.0)
        self.assertEqual(discovered[0]["best_ask"], 8250.0)
        self.assertEqual(discovered[0]["spread_bps"], 0.0)

    def test_indodax_market_health_counts_healthy_pairs(self):
        payload = {
            "tickers": {
                "btc_idr": {"buy": "100", "sell": "101", "last": "100.5", "vol_idr": "1000000"},
                "xrp_idr": {"buy": "10", "sell": "20", "last": "15", "vol_idr": "900000"},
            }
        }

        health = paper_only_indodax_market_health(payload, max_markets=10)

        self.assertEqual(health["market_count"], 2)
        self.assertEqual(health["healthy_market_count"], 1)
        self.assertEqual(health["quotes"], ["IDR"])

    def test_classify_fiat_corridor_keeps_indodax_idr_as_local_fiat(self):
        result = classify_fiat_corridor("BTC", "IDR", venue_name="INDODAX", venue_notes="local fiat")
        self.assertEqual(result["corridor_type"], "local_fiat")
        self.assertGreaterEqual(result["corridor_confidence"], 0.72)
