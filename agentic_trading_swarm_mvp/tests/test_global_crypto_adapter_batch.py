from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as frontier
from adapters.registry import discover_adapters
from adapters.venues.crypto_derivatives import funding_candidate
from adapters.venues.deribit import _normalize_deribit_book, parse_deribit_summaries
from adapters.venues.whitebit import parse_whitebit_futures
from agent_review import review_candidate
from frontier_data_quality import _extract_depth
from route_resolver import enrich_candidate_with_route
from settings import DEFAULT_SETTINGS
from storage import signal_key


def fetch_result(payload: object) -> dict:
    return {
        "ok": True,
        "data_status": "reachable",
        "status": "reachable",
        "http_status": 200,
        "latency_ms": 12.0,
        "payload": payload,
        "received_at": "2026-07-31T12:00:00+00:00",
    }


class FrontierVenueParserTests(unittest.TestCase):
    def test_coinjar_products_fetches_and_normalizes_public_ticker(self) -> None:
        target = {
            "venue": "COINJAR",
            "market_type": "spot",
            "route_id": "coinjar_spot_public",
            "url": "https://api.exchange.coinjar.com/products",
            "quote_assets": ["AUD", "USD", "USDC", "USDT"],
            "max_product_tickers": 2,
        }
        products = [
            {
                "id": "BTCAUD",
                "base_currency": {"iso_code": "BTC"},
                "counter_currency": {"iso_code": "AUD"},
            }
        ]
        ticker = fetch_result(
            {
                "last": "100000",
                "bid": "99990",
                "ask": "100010",
                "volume_24h": "12",
                "change_24h": "0.02",
                "status": "continuous",
                "current_time": "2026-07-31T12:00:00Z",
            }
        )
        with mock.patch.object(frontier, "fetch_json", return_value=ticker):
            rows = frontier._parse_coinjar_products(target, fetch_result(products))
        self.assertEqual(1, len(rows))
        self.assertEqual("BTC", rows[0]["base"])
        self.assertEqual("AUD", rows[0]["quote"])
        self.assertEqual(100000.0, rows[0]["last"])
        self.assertLess(rows[0]["spread_bps"], 3.0)

    def test_ripio_tickers_normalize_latam_pairs(self) -> None:
        target = {
            "venue": "RIPIO",
            "market_type": "spot",
            "route_id": "ripio_spot_public",
            "url": "https://api.ripio.com/trade/public/tickers",
            "quote_assets": ["ARS", "BRL", "MXN", "USD", "USDT"],
        }
        rows = frontier._parse_ripio_tickers(
            target,
            fetch_result(
                {
                    "data": [
                        {
                            "pair": "BTC_BRL",
                            "base_code": "BTC",
                            "quote_code": "BRL",
                            "last": "350000",
                            "bid": "349900",
                            "ask": "350100",
                            "quote_volume": "2000000",
                            "price_change_percent_24h": "1.5",
                            "date": "2026-07-31T12:00:00Z",
                            "is_frozen": False,
                        }
                    ]
                }
            ),
        )
        self.assertEqual("RIPIO:BTC_BRL", rows[0]["instrument_id"])
        self.assertEqual("BRL", rows[0]["quote"])
        self.assertEqual(2000000.0, rows[0]["quote_volume_24h"])

    def test_whitebit_spot_parser_excludes_perpetuals(self) -> None:
        target = {
            "venue": "WHITEBIT",
            "market_type": "spot",
            "route_id": "whitebit_spot_public",
            "url": "https://whitebit.com/api/v4/public/ticker",
            "quote_assets": ["USD", "USDC", "USDT"],
        }
        rows = frontier._parse_whitebit_tickers(
            target,
            fetch_result(
                {
                    "BTC_USDT": {"last_price": "63000", "quote_volume": "1000000", "change": "1.2"},
                    "BTC_PERP": {"last_price": "63010", "quote_volume": "2000000", "change": "1.3"},
                }
            ),
        )
        self.assertEqual(["BTC_USDT"], [row["symbol"] for row in rows])

    def test_new_depth_payloads_are_supported(self) -> None:
        coinjar = _extract_depth("coinjar_book", {"bids": [[10, 2]], "asks": [[11, 3]]}, "now")
        ripio = _extract_depth(
            "ripio_level2",
            {"data": {"bids": [{"price": "10", "amount": "2"}], "asks": [{"price": "11", "amount": "3"}]}},
            "now",
        )
        whitebit = _extract_depth(
            "whitebit_depth",
            {"bids": [["10", "2"]], "asks": [["11", "3"]], "timestamp": 1785513600},
            "now",
        )
        self.assertEqual([[10, 2]], coinjar["bids"])
        self.assertEqual("10", ripio["bids"][0]["price"])
        self.assertEqual("exchange_timestamp", whitebit["freshness_basis"])


class DerivativesAdapterTests(unittest.TestCase):
    def test_plugins_are_registered(self) -> None:
        found = set(discover_adapters())
        self.assertIn("whitebit_perpetuals_public", found)
        self.assertIn("deribit_derivatives_public", found)

    def test_whitebit_futures_normalization(self) -> None:
        rows = parse_whitebit_futures(
            {
                "result": [
                    {
                        "ticker_id": "BTC_PERP",
                        "product_type": "Perpetual",
                        "stock_currency": "BTC",
                        "money_currency": "USDT",
                        "last_price": "63000",
                        "bid": "62999",
                        "ask": "63001",
                        "money_volume": "50000000",
                        "open_interest": "100000",
                        "index_price": "62990",
                        "funding_rate": "0.0005",
                        "funding_interval_minutes": 480,
                        "next_funding_rate_timestamp": 1785542400000,
                    }
                ]
            },
            observed_at="2026-07-31T12:00:00+00:00",
        )
        self.assertEqual("WHITEBIT:BTC_PERP", rows[0]["inst_id"])
        self.assertEqual(5.0, rows[0]["funding_bps"])
        self.assertEqual(8.0, rows[0]["funding_interval_hours"])

    def test_deribit_future_and_option_are_kept_separate(self) -> None:
        future = parse_deribit_summaries(
            {
                "result": [
                    {
                        "instrument_name": "BTC-PERPETUAL",
                        "base_currency": "BTC",
                        "quote_currency": "USD",
                        "last": 63000,
                        "mark_price": 63001,
                        "bid_price": 62999,
                        "ask_price": 63001,
                        "estimated_delivery_price": 62990,
                        "funding_8h": 0.0004,
                        "volume_usd": 10000000,
                    }
                ]
            },
            kind="future",
        )[0]
        option = parse_deribit_summaries(
            {
                "result": [
                    {
                        "instrument_name": "BTC-25DEC26-70000-C",
                        "base_currency": "BTC",
                        "quote_currency": "BTC",
                        "mark_price": 0.1,
                        "bid_price": 0.09,
                        "ask_price": 0.11,
                        "mark_iv": 55.0,
                        "underlying_price": 63000,
                        "volume_usd": 100000,
                    }
                ]
            },
            kind="option",
        )[0]
        self.assertEqual("perp_funding_basis", future["trade_type"])
        self.assertEqual(4.0, future["funding_bps"])
        self.assertEqual("crypto_options_volatility_research", option["trade_type"])
        self.assertEqual("options_strategy_hypothesis_required", option["candidate_reject_reason"])

    def test_deribit_contract_depth_is_converted_to_base_equivalent(self) -> None:
        book = _normalize_deribit_book(
            {"result": {"timestamp": 1785513600000, "bids": [[100.0, 1000.0]], "asks": [[101.0, 2020.0]]}}
        )
        self.assertEqual(10.0, book["bids"][0][1])
        self.assertEqual(20.0, book["asks"][0][1])

    def test_under_specified_public_funding_candidate_is_blocked_before_paper_trade(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        observation = {
            "venue": "DERIBIT",
            "inst_id": "DERIBIT:BTC-PERPETUAL",
            "symbol": "BTC-PERPETUAL",
            "base": "BTC",
            "quote": "USD",
            "last": 63000.0,
            "bid": 62999.0,
            "ask": 63001.0,
            "index_price": 62995.0,
            "funding_rate": 0.003,
            "funding_bps": 30.0,
            "funding_interval_hours": 8.0,
            "quote_volume_24h": 100000000.0,
            "change_24h_pct": 1.0,
            "quality_status": "verified",
            "quality_score": 90.0,
            "simulated_fills": {
                "buy": {"1000": {"filled": True, "slippage_bps": 1.0}},
                "sell": {"1000": {"filled": True, "slippage_bps": 1.0}},
            },
            "anomaly_flags": [],
            "critical_anomaly_flags": [],
            "observed_at": "2026-07-31T12:00:00+00:00",
            "data_status": "reachable",
            "source_url": "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        }
        candidate = funding_candidate(observation, settings)
        self.assertIsNotNone(candidate)
        candidate = enrich_candidate_with_route(candidate, settings)
        self.assertEqual("standard", candidate["execution_feasibility"]["status"])
        self.assertEqual("public_crypto_derivatives_paper", candidate["route_id"])
        self.assertTrue(signal_key(candidate).startswith("public_perpetual_funding_capture_v1|DERIBIT|"))
        self.assertEqual(0.0, candidate["score"])
        self.assertFalse(candidate["paper_route_eligibility"]["route_eligible"])
        self.assertEqual(
            {
                "hedge_venue",
                "hedge_instrument",
                "paper_leg_mapping_valid",
                "venue_capabilities",
            },
            set(candidate["paper_route_eligibility"]["missing_prerequisites"]),
        )

        review = review_candidate(candidate, settings, adjustments={})
        self.assertEqual("reject", review["decision"])
        self.assertTrue(
            any("paper route eligibility blocked" in block for block in review["hard_blocks"])
        )

    def test_long_perpetual_funding_does_not_require_spot_borrow(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["account_capabilities"]["spot_borrow"] = False
        candidate = {
            "venue": "WHITEBIT",
            "inst_id": "WHITEBIT:BTC_PERP",
            "trade_type": "perp_funding_basis",
            "direction": "funding_capture_long_perp",
            "data_status": "reachable",
        }
        routed = enrich_candidate_with_route(candidate, settings)
        self.assertEqual("standard", routed["execution_feasibility"]["status"])
        self.assertNotIn("spot_borrow", routed["execution_feasibility"]["missing_requirements"])

    def test_regional_spot_adapter_can_reach_paper_review_after_quality_gates(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        observation = {
            "venue": "COINJAR",
            "market_type": "spot",
            "region": "Australia",
            "symbol": "BTCAUD",
            "instrument_id": "COINJAR:BTCAUD",
            "base": "BTC",
            "quote": "AUD",
            "comparison_key": "BTC",
            "last": 100000.0,
            "usd_normalized_last": 98.0,
            "spread_bps": 999.0,
            "book_levels": {"bids": [[99990.0, 20.0]], "asks": [[100010.0, 20.0]]},
            "quote_to_usd_multiplier": 0.00098,
            "simulated_fills": {
                "buy": {"1000": {"filled": True, "slippage_bps": 1.0}},
                "sell": {"1000": {"filled": True, "slippage_bps": 1.0}},
            },
            "quote_volume_24h": 5000000.0,
            "data_status": "reachable",
            "http_status": 200,
            "latency_ms": 20.0,
            "route_id": "coinjar_spot_public",
            "source_url": "https://api.exchange.coinjar.com/products",
            "last_checked_at": "2026-07-31T12:00:00+00:00",
            "quote_normalization_status": "external_fx_reference",
            "quote_normalization_source": "public_fx_reference",
            "local_quote_observe_only": False,
            "verified_depth_snapshot_count": 3,
            "quality_status": "verified",
            "quality_score": 80.0,
            "freshness_age_seconds": 1.0,
            "anomaly_flags": [],
            "critical_anomaly_flags": [],
        }
        candidate = frontier._candidate_from_observation(observation, settings, 100.0, 4)
        self.assertEqual("long_frontier_spot", candidate["direction"])
        self.assertEqual("passed", candidate["regional_candidate_gate_status"])
        self.assertFalse(candidate["paper_entry_blocked"])
        routed = enrich_candidate_with_route(candidate, settings)
        review = review_candidate(routed, settings, adjustments={})
        self.assertEqual("approve_paper_trade", review["decision"])


if __name__ == "__main__":
    unittest.main()
