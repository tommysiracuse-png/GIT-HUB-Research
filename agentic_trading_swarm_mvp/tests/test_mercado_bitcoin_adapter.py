import unittest

from src.frontier_crypto_adapter import (
    paper_only_mercado_bitcoin_market_catalog,
    paper_only_mercado_bitcoin_normalize_symbol,
    paper_only_mercado_bitcoin_observation_from_public_payloads,
)


class TestMercadoBitcoinAdapter(unittest.TestCase):
    def test_normalize_symbol_accepts_common_forms(self):
        self.assertEqual(paper_only_mercado_bitcoin_normalize_symbol("BTC/BRL"), "BTCBRL")
        self.assertEqual(paper_only_mercado_bitcoin_normalize_symbol("eth-brl"), "ETHBRL")
        self.assertEqual(paper_only_mercado_bitcoin_normalize_symbol("BTC"), "BTCBRL")
        self.assertIsNone(paper_only_mercado_bitcoin_normalize_symbol("DOGE/BRL"))

    def test_market_catalog_exposes_brazil_spot_markets(self):
        catalog = paper_only_mercado_bitcoin_market_catalog()
        markets = {item["market"]: item for item in catalog}

        self.assertIn("MERCADO_BITCOIN:BTC/BRL", markets)
        self.assertIn("MERCADO_BITCOIN:ETH/BRL", markets)
        self.assertEqual(markets["MERCADO_BITCOIN:BTC/BRL"]["endpoints"]["ticker"], "https://www.mercadobitcoin.net/api/BTC/ticker/")
        self.assertTrue(markets["MERCADO_BITCOIN:BTC/BRL"]["paper_only"])

    def test_observation_builds_quality_and_liquidity_fields(self):
        observation = paper_only_mercado_bitcoin_observation_from_public_payloads(
            "BTC/BRL",
            ticker_payload={
                "ticker": {
                    "buy": "610000.00",
                    "sell": "610500.00",
                    "last": "610100.00",
                    "vol": "12.5",
                }
            },
            orderbook_payload={
                "bids": [["610000.00", "0.42"]],
                "asks": [["610500.00", "0.36"]],
            },
            quote_timestamp="2026-07-22T02:36:59+00:00",
            evaluation_timestamp="2026-07-22T02:36:59+00:00",
            route_status="reachable",
            intended_paper_notional_usd=1000.0,
            venue_spread_baseline_bps=20.0,
        )

        self.assertEqual(observation["venue"], "MERCADO_BITCOIN")
        self.assertEqual(observation["symbol"], "BTC/BRL")
        self.assertAlmostEqual(observation["last_price"], 610100.0)
        self.assertAlmostEqual(observation["best_bid"], 610000.0)
        self.assertAlmostEqual(observation["best_ask"], 610500.0)
        self.assertGreater(observation["spread_bps"], 0.0)
        self.assertEqual(observation["freshness_timestamp"], "2026-07-22T02:36:59+00:00")
        self.assertEqual(observation["route_status"], "reachable")
        self.assertIsInstance(observation["route_quality"], dict)
        self.assertIsNotNone(observation["depth_liquidity_score"])
        self.assertGreaterEqual(observation["depth_liquidity_score"], 0.0)
        self.assertLessEqual(observation["depth_liquidity_score"], 1.0)
        self.assertEqual(observation["market_data_origin"], "native_public_spot")
        self.assertEqual(observation["venue_constraints"]["price_precision"], 0)
        self.assertEqual(observation["venue_constraints"]["quantity_precision"], 2)
        self.assertEqual(observation["venue_quality"]["route_status"], "reachable")

    def test_observation_respects_blocked_route_status(self):
        observation = paper_only_mercado_bitcoin_observation_from_public_payloads(
            "ETH/BRL",
            ticker_payload={"ticker": {"last": "19000.00"}},
            orderbook_payload={
                "bids": [["18990.00", "1.0"]],
                "asks": [["19010.00", "1.2"]],
            },
            quote_timestamp="2026-07-22T02:36:59+00:00",
            evaluation_timestamp="2026-07-22T02:36:59+00:00",
            route_status="maintenance",
            intended_paper_notional_usd=500.0,
        )

        self.assertTrue(observation["paper_ineligible"])
        self.assertEqual(observation["venue_quality"]["route_status"], "maintenance")


if __name__ == "__main__":
    unittest.main()
