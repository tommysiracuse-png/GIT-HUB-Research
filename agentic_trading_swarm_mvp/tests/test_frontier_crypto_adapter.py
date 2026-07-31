from __future__ import annotations

import copy
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as frontier
import route_resolver
import signal_redesign
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["frontier_crypto_adapter"]["min_dislocation_bps"] = 12.0
    return cfg


def result(payload: object) -> dict:
    return {
        "ok": True,
        "data_status": "reachable",
        "http_status": "200",
        "latency_ms": 12.3,
        "payload": payload,
    }


class FrontierCryptoAdapterTests(unittest.TestCase):
    def test_scan_batch_can_defer_preliminary_report_until_quality_enrichment(self) -> None:
        cfg = settings()
        observation = self._obs("A", "ABC-USDT", "ABC", "USDT", 100, 100000)
        candidate = frontier._candidate_from_observation(observation, cfg, 95, 3)
        with (
            mock.patch.object(frontier, "scan_venues", return_value=[observation]),
            mock.patch.object(frontier, "_select_observations", return_value=[observation]),
            mock.patch.object(frontier, "_reference_prices", return_value={"ABC": 95.0}),
            mock.patch.object(frontier, "_candidate_from_observation", return_value=candidate),
            mock.patch.object(frontier, "write_outputs") as write_outputs,
        ):
            deferred = frontier.build_scan_batch(
                cfg,
                conn=object(),
                write_preliminary_report=False,
            )
            self.assertEqual(1, len(deferred.candidates))
            write_outputs.assert_not_called()

            frontier.build_scan_batch(
                cfg,
                conn=object(),
                write_preliminary_report=True,
            )
            write_outputs.assert_called_once()

    def test_public_venue_parsers_normalize_symbols(self) -> None:
        cases = [
            (
                frontier._parse_kucoin_all_tickers,
                {"venue": "KUCOIN", "market_type": "spot", "route_id": "kucoin_spot_public", "url": "u"},
                {"data": {"ticker": [{"symbol": "ABC-USDT", "buy": "9.99", "sell": "10.01", "last": "10", "volValue": "100000"}]}},
            ),
            (
                frontier._parse_gate_spot_tickers,
                {"venue": "GATE", "market_type": "spot", "route_id": "gate_spot_public", "url": "u"},
                [{"currency_pair": "ABC_USDT", "highest_bid": "9.99", "lowest_ask": "10.01", "last": "10", "quote_volume": "100000"}],
            ),
            (
                frontier._parse_mexc_24hr,
                {"venue": "MEXC", "market_type": "spot", "route_id": "mexc_spot_public", "url": "u"},
                [{"symbol": "ABCUSDT", "bidPrice": "9.99", "askPrice": "10.01", "lastPrice": "10", "quoteVolume": "100000"}],
            ),
            (
                frontier._parse_bitget_spot_tickers,
                {"venue": "BITGET", "market_type": "spot", "route_id": "bitget_spot_public", "url": "u"},
                {"data": [{"symbol": "ABCUSDT_SPBL", "bestBid": "9.99", "bestAsk": "10.01", "close": "10", "quoteVol": "100000"}]},
            ),
        ]

        for parser, target, payload in cases:
            with self.subTest(target=target["venue"]):
                rows = parser(target, result(payload))
                self.assertEqual(rows[0]["base"], "ABC")
                self.assertEqual(rows[0]["quote"], "USDT")
                self.assertEqual(rows[0]["last"], 10.0)
                self.assertGreater(rows[0]["quote_volume_24h"], 0)

    def test_regional_venue_parsers_normalize_local_quotes(self) -> None:
        cases = [
            (
                frontier._parse_luno_tickers,
                {"venue": "LUNO", "market_type": "spot", "route_id": "luno_spot_public", "url": "u", "region": "africa"},
                {"tickers": [{"pair": "XBTZAR", "bid": "899000", "ask": "901000", "last_trade": "900000", "rolling_24_hour_volume": "2"}]},
                ("BTC", "ZAR"),
            ),
            (
                frontier._parse_indodax_ticker_all,
                {"venue": "INDODAX", "market_type": "spot", "route_id": "indodax_spot_public", "url": "u", "region": "southeast_asia"},
                {"tickers": {"btc_idr": {"buy": "999000000", "sell": "1001000000", "last": "1000000000", "vol_idr": "10000000000"}}},
                ("BTC", "IDR"),
            ),
            (
                frontier._parse_bitkub_ticker,
                {"venue": "BITKUB", "market_type": "spot", "route_id": "bitkub_spot_public", "url": "u", "region": "southeast_asia"},
                {"THB_BTC": {"highestBid": "1799000", "lowestAsk": "1801000", "last": "1800000", "baseVolume": "1"}},
                ("BTC", "THB"),
            ),
        ]

        for parser, target, payload, expected in cases:
            with self.subTest(target=target["venue"]):
                rows = parser(target, result(payload))
                self.assertEqual((rows[0]["base"], rows[0]["quote"]), expected)
                self.assertEqual(rows[0]["region"], target["region"])
                self.assertGreater(rows[0]["last"], 0)

    def test_latam_public_parsers_create_normalized_observations(self) -> None:
        old_fetch = frontier.fetch_json

        def fake_fetch(url: str, timeout: int = 8) -> dict:
            if "bitso.com/v3/ticker" in url:
                return result(
                    {
                        "payload": {
                            "book": "btc_mxn",
                            "bid": "1099000",
                            "ask": "1101000",
                            "last": "1100000",
                            "volume": "2",
                        }
                    }
                )
            if "mercadobitcoin" in url:
                return result({"bids": [["329000", "0.5"]], "asks": [["330000", "0.4"]]})
            if "buda.com" in url:
                return result(
                    {
                        "ticker": {
                            "last_price": ["57685535.0", "CLP"],
                            "max_bid": ["57456189.0", "CLP"],
                            "min_ask": ["57801039.0", "CLP"],
                            "quote_volume": ["91471262.93", "CLP"],
                        }
                    }
                )
            raise AssertionError(url)

        frontier.fetch_json = fake_fetch
        try:
            bitso = frontier._parse_bitso_available_books(
                {"venue": "BITSO", "market_type": "spot", "route_id": "bitso_spot_public", "url": "u", "region": "LATAM"},
                result({"payload": [{"book": "btc_mxn"}]}),
            )
            mercado = frontier._parse_mercado_bitcoin_symbols(
                {"venue": "MERCADO_BITCOIN", "market_type": "spot", "route_id": "mercado_spot_public", "url": "u", "region": "LATAM"},
                result({"symbol": ["BTC-BRL"]}),
            )
            buda = frontier._parse_buda_markets(
                {"venue": "BUDA", "market_type": "spot", "route_id": "buda_spot_public", "url": "u", "region": "LATAM"},
                result({"markets": [{"id": "BTC-CLP", "disabled": False}]}),
            )
        finally:
            frontier.fetch_json = old_fetch

        self.assertEqual((bitso[0]["base"], bitso[0]["quote"]), ("BTC", "MXN"))
        self.assertEqual((mercado[0]["base"], mercado[0]["quote"]), ("BTC", "BRL"))
        self.assertEqual((buda[0]["base"], buda[0]["quote"]), ("BTC", "CLP"))
        self.assertGreater(mercado[0]["spread_bps"], 0)

    def test_broad_exchange_parsers_expand_single_health_checks(self) -> None:
        old_fetch = frontier.fetch_json

        def fake_fetch(url: str, timeout: int = 8) -> dict:
            if "coinbase" in url:
                return result({"bid": "99", "ask": "101", "price": "100", "volume": "10"})
            if "AssetPairs" in url:
                return result({"result": {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "BTC/USD"}}})
            raise AssertionError(url)

        frontier.fetch_json = fake_fetch
        try:
            coinbase = frontier._parse_coinbase_products(
                {"venue": "COINBASE", "market_type": "spot", "route_id": "coinbase_spot_public", "url": "u"},
                result([{"id": "BTC-USD", "quote_currency": "USD", "status": "online", "trading_disabled": False}]),
            )
            kraken = frontier._parse_kraken_all_tickers(
                {
                    "venue": "KRAKEN",
                    "market_type": "spot",
                    "route_id": "kraken_spot_public",
                    "url": "u",
                    "asset_pairs_url": "https://api.kraken.com/0/public/AssetPairs",
                },
                result({"result": {"XXBTZUSD": {"b": ["99"], "a": ["101"], "c": ["100"], "v": ["1", "10"]}}}),
            )
            okx = frontier._parse_okx_spot_tickers(
                {"venue": "OKX_SPOT", "market_type": "spot", "route_id": "okx_spot_public", "url": "u"},
                result({"data": [{"instId": "BTC-USDT", "bidPx": "99", "askPx": "101", "last": "100", "volCcy24h": "100000"}]}),
            )
        finally:
            frontier.fetch_json = old_fetch

        self.assertEqual((coinbase[0]["base"], coinbase[0]["quote"]), ("BTC", "USD"))
        self.assertEqual((kraken[0]["base"], kraken[0]["quote"]), ("BTC", "USD"))
        self.assertEqual((okx[0]["base"], okx[0]["quote"]), ("BTC", "USDT"))

    def test_regional_quotes_need_same_venue_stablecoin_reference(self) -> None:
        cfg = settings()
        observations = [
            self._obs("LUNO", "USDTZAR", "USDT", "ZAR", 18.0, 900000, region="africa"),
            self._obs("LUNO", "XBTZAR", "BTC", "ZAR", 1_800_000.0, 1_800_000, region="africa"),
            self._obs("COINBASE", "BTC-USD", "BTC", "USD", 100_000.0, 10_000_000),
        ]

        normalized = frontier._normalize_regional_quotes(observations)
        btc_luno = next(row for row in normalized if row["instrument_id"] == "LUNO:XBTZAR")
        usdt_luno = next(row for row in normalized if row["instrument_id"] == "LUNO:USDTZAR")
        refs = frontier._reference_prices(normalized, cfg)
        candidate = frontier._candidate_from_observation(
            btc_luno,
            cfg,
            refs["BTC"],
            2,
        )

        self.assertAlmostEqual(btc_luno["usd_normalized_last"], 100_000.0)
        self.assertEqual(btc_luno["quote_normalization_status"], "same_venue_stablecoin_reference")
        self.assertFalse(btc_luno["local_quote_observe_only"])
        self.assertTrue(usdt_luno["local_quote_observe_only"])
        self.assertEqual(candidate["region"], "africa")
        self.assertEqual(candidate["quote_normalization_status"], "same_venue_stablecoin_reference")

    def test_missing_regional_reference_is_observe_only(self) -> None:
        cfg = settings()
        observations = [
            self._obs("BITKUB", "THB_BTC", "BTC", "THB", 1_800_000.0, 1_800_000, region="southeast_asia"),
            self._obs("COINBASE", "BTC-USD", "BTC", "USD", 100_000.0, 10_000_000),
        ]

        normalized = frontier._normalize_regional_quotes(observations)
        bitkub = next(row for row in normalized if row["venue"] == "BITKUB")
        candidate = frontier._candidate_from_observation(bitkub, cfg, 100_000.0, 2)

        self.assertTrue(bitkub["local_quote_observe_only"])
        self.assertEqual(bitkub["quote_normalization_status"], "missing_same_venue_stablecoin_reference")
        self.assertEqual(candidate["direction"], "watch_only")
        self.assertEqual(candidate["candidate_reject_reason"], "local_quote_observe_only")

    def test_external_fx_reference_normalizes_but_requires_repeated_verified_depth(self) -> None:
        cfg = settings()
        observations = [
            self._obs("LUNO", "XBTZAR", "BTC", "ZAR", 1_800_000.0, 1_800_000, region="africa"),
            self._obs("COINBASE", "BTC-USD", "BTC", "USD", 100_000.0, 10_000_000),
        ]

        normalized = frontier._normalize_regional_quotes(
            observations,
            fx_references={
                "ZAR": {
                    "rate": 18.0,
                    "provider": "ExchangeRate-API Open",
                    "age_seconds": 10.0,
                    "source_url": "https://open.er-api.com/v6/latest/USD",
                }
            },
        )
        luno = next(row for row in normalized if row["venue"] == "LUNO")
        luno.update(
            {
                "quality_status": "verified",
                "quality_score": 90.0,
                "verified_depth_snapshot_count": 1,
                "simulated_fills": {
                    "buy": {"1000": {"filled": True, "slippage_bps": 1.0}},
                    "sell": {"1000": {"filled": True, "slippage_bps": 1.0}},
                },
                "anomaly_flags": [],
                "critical_anomaly_flags": [],
            }
        )

        candidate = frontier._candidate_from_observation(luno, cfg, 99_000.0, 2)
        luno["verified_depth_snapshot_count"] = 3
        ready = frontier._candidate_from_observation(luno, cfg, 99_000.0, 2)

        self.assertAlmostEqual(luno["usd_normalized_last"], 100_000.0)
        self.assertEqual(luno["quote_normalization_status"], "external_fx_reference")
        self.assertEqual(candidate["regional_candidate_gate_status"], "insufficient_verified_depth_snapshots")
        self.assertTrue(candidate["paper_entry_blocked"])
        self.assertEqual(ready["regional_candidate_gate_status"], "passed")
        self.assertFalse(ready["paper_entry_blocked"])

    def test_selection_filters_stables_and_unsupported_quotes(self) -> None:
        registry = {
            "filters": {
                "quote_assets": ["USD", "USDT", "USDC"],
                "exclude_base_assets": ["USDT", "USDC", "USD"],
                "top_volume_per_venue": 80,
                "frontier_symbols_per_venue": 40,
                "frontier_max_listing_count": 3,
                "min_frontier_quote_volume_usd": 25000,
            }
        }
        rows = [
            self._obs("KUCOIN", "ABC-USDT", "ABC", "USDT", 10, 100000),
            self._obs("KUCOIN", "USDC-USDT", "USDC", "USDT", 1, 100000),
            self._obs("KUCOIN", "ABC-BTC", "ABC", "BTC", 0.0001, 100000),
        ]

        selected = frontier._select_observations(rows, registry)

        self.assertEqual([row["symbol"] for row in selected], ["ABC-USDT"])

    def test_cross_venue_dislocation_creates_long_and_short_candidates(self) -> None:
        cfg = settings()
        observations = [
            self._obs("A", "ABC-USDT", "ABC", "USDT", 100, 100000),
            self._obs("B", "ABC-USDT", "ABC", "USDT", 90, 100000),
            self._obs("C", "ABC-USDT", "ABC", "USDT", 110, 100000),
        ]
        refs = frontier._reference_prices(observations, cfg)
        candidates = [
            frontier._candidate_from_observation(row, cfg, refs["ABC"], 3)
            for row in observations
        ]
        directions = {row["venue"]: row["direction"] for row in candidates}

        self.assertEqual(directions["B"], "long_frontier_spot")
        self.assertEqual(directions["C"], "short_frontier_spot")
        self.assertEqual(directions["A"], "watch_only")

    def test_blocked_observation_is_health_evidence_not_candidate(self) -> None:
        cfg = settings()
        observations = [
            self._obs("A", "ABC-USDT", "ABC", "USDT", 100, 100000),
            self._obs("B", "ABC-USDT", "ABC", "USDT", 110, 100000),
            self._obs("BLOCKED", "ABC-USDT", "ABC", "USDT", 0, 0, data_status="blocked"),
        ]
        refs = frontier._reference_prices(observations, cfg)
        candidates = [
            frontier._candidate_from_observation(row, cfg, refs.get(row["comparison_key"]), 2)
            for row in observations
            if row.get("data_status") == "reachable" and row.get("comparison_key") in refs
        ]

        self.assertEqual(len(candidates), 2)
        self.assertNotIn("BLOCKED", {row["venue"] for row in candidates})
        self.assertEqual(frontier.summarize(observations, candidates)["blocked_venue_count"], 1)

    def test_report_contains_counts_dislocations_and_route_blockers(self) -> None:
        cfg = settings()
        observations = [
            self._obs("A", "ABC-USDT", "ABC", "USDT", 100, 100000),
            self._obs("B", "ABC-USDT", "ABC", "USDT", 90, 100000),
            self._obs("C", "ABC-USDT", "ABC", "USDT", 110, 100000),
        ]
        refs = frontier._reference_prices(observations, cfg)
        candidates = [
            frontier._candidate_from_observation(row, cfg, refs["ABC"], 3)
            for row in observations
        ]

        with tempfile.TemporaryDirectory() as tmp:
            old_json = frontier.REPORT_JSON
            old_md = frontier.REPORT_MD
            frontier.REPORT_JSON = pathlib.Path(tmp) / "frontier.json"
            frontier.REPORT_MD = pathlib.Path(tmp) / "frontier.md"
            try:
                report = frontier.write_outputs(
                    observations,
                    candidates,
                    cfg,
                    quality_summary={
                        "selected_count": 3,
                        "enriched_count": 2,
                        "selected_by_venue": {"A": 1, "B": 1, "C": 1},
                        "selection_limits": {"max_symbols_per_cycle": 300, "max_symbols_per_venue": 32},
                        "worker_count": 16,
                        "venue_quota_report": {
                            "A": {
                                "target_selected_this_cycle": 2,
                                "selected_this_cycle": 1,
                                "status": "partial",
                                "missed_reason": "insufficient_reachable_observations",
                            }
                        },
                    },
                )
            finally:
                frontier.REPORT_JSON = old_json
                frontier.REPORT_MD = old_md

        summary = report["summary"]
        self.assertEqual(summary["venue_count"], 3)
        self.assertEqual(summary["symbol_count"], 1)
        self.assertTrue(summary["top_dislocations"])
        self.assertIn("spot_borrow", summary["by_route_blocker"])
        self.assertIn("candidate_activity", summary)
        self.assertEqual(summary["expansion_map"]["worker_count"], 16)
        self.assertEqual(summary["expansion_map"]["selection_limits"]["max_symbols_per_cycle"], 300)
        self.assertEqual(summary["expansion_map"]["venue_quota_report"]["A"]["status"], "partial")

    def test_route_resolver_compatibility_for_frontier_candidate(self) -> None:
        cfg = settings()
        observation = self._obs("C", "ABC-USDT", "ABC", "USDT", 110, 100000)
        candidate = frontier._candidate_from_observation(observation, cfg, 100, 3)
        route = route_resolver.resolve_candidate_route(candidate, cfg)

        self.assertEqual(candidate["trade_type"], "frontier_crypto_venue_map")
        self.assertEqual(candidate["direction"], "short_frontier_spot")
        self.assertEqual(route["route_status"], "conditional")
        self.assertIn("spot_borrow", route["missing_permissions"])
        self.assertNotIn("crypto_derivatives", route["missing_permissions"])

    def test_systemic_short_variant_gates_quality_route_and_direction(self) -> None:
        cfg = settings()
        variant = next(row for row in signal_redesign.DEFAULT_VARIANTS if row["variant_id"] == "frontier_v5_short_route_quality")
        observations = [
            self._quality_obs("A", "ABC-USDT", "ABC", "USDT", 100, 20_000_000, quality_score=90),
            self._quality_obs("B", "ABC-USDT", "ABC", "USDT", 90, 20_000_000, quality_score=90),
            self._quality_obs("C", "ABC-USDT", "ABC", "USDT", 110, 20_000_000, quality_score=90),
        ]

        candidates = frontier.build_variant_candidates(
            observations,
            cfg,
            "frontier_v5_short_route_quality",
            variant["config"],
        )
        actionable = [row for row in candidates if row["direction"] != "watch_only"]

        self.assertEqual({row["direction"] for row in actionable}, {"short_frontier_spot"})
        self.assertTrue(all(row["quality_score"] >= 65 for row in actionable))
        self.assertTrue(all(row["frontier_cost_source"] == "public_order_book" for row in actionable))
        self.assertTrue(any(row.get("candidate_reject_reason") == "direction_not_enabled" for row in candidates))

    def test_systemic_variant_blocks_regional_quotes_when_disabled(self) -> None:
        cfg = settings()
        variant = next(row for row in signal_redesign.DEFAULT_VARIANTS if row["variant_id"] == "frontier_v5_short_route_quality")
        observations = [
            self._quality_obs("LUNO", "BTC-USDT", "BTC", "USDT", 110_000, 20_000_000, quality_score=90, region="Africa"),
            self._quality_obs("A", "BTC-USDT", "BTC", "USDT", 100_000, 20_000_000, quality_score=90),
            self._quality_obs("B", "BTC-USDT", "BTC", "USDT", 100_500, 20_000_000, quality_score=90),
        ]
        for row in observations:
            row["usd_normalized_last"] = row["last"]
            row["comparison_price"] = row["last"]
            row["quote_normalization_status"] = "usd_like"

        candidates = frontier.build_variant_candidates(
            observations,
            cfg,
            "frontier_v5_short_route_quality",
            variant["config"],
        )
        luno = next(row for row in candidates if row["venue"] == "LUNO")

        self.assertEqual(luno["direction"], "watch_only")
        self.assertEqual(luno["candidate_reject_reason"], "regional_quote_not_enabled")

    def test_bybit_spot_long_expansion_variant_is_long_only_and_quality_gated(self) -> None:
        cfg = settings()
        variant = next(row for row in signal_redesign.DEFAULT_VARIANTS if row["variant_id"] == "frontier_v14_bybit_spot_long_expansion")
        observations = [
            self._quality_obs("BYBIT_SPOT", "ABCUSDT", "ABC", "USDT", 90, 20_000_000, quality_score=80),
            self._quality_obs("GATE", "ABC_USDT", "ABC", "USDT", 100, 20_000_000, quality_score=90),
            self._quality_obs("MEXC", "ABCUSDT", "ABC", "USDT", 101, 20_000_000, quality_score=90),
            self._quality_obs("COINBASE", "ABC-USD", "ABC", "USD", 100.5, 20_000_000, quality_score=90),
        ]

        candidates = frontier.build_variant_candidates(
            observations,
            cfg,
            "frontier_v14_bybit_spot_long_expansion",
            variant["config"],
        )
        actionable = [row for row in candidates if row["direction"] != "watch_only"]

        self.assertEqual([row["venue"] for row in actionable], ["BYBIT_SPOT"])
        self.assertEqual(actionable[0]["direction"], "long_frontier_spot")
        self.assertEqual(actionable[0]["variant_route_status"], "standard")
        self.assertTrue(actionable[0]["promotion_eligible"])

    def _obs(
        self,
        venue: str,
        symbol: str,
        base: str,
        quote: str,
        last: float,
        quote_volume: float,
        data_status: str = "reachable",
        region: str | None = None,
    ) -> dict:
        bid = last * 0.9999 if last else None
        ask = last * 1.0001 if last else None
        return frontier._finalize_observation(
            {
                "venue": venue,
                "market_type": "spot",
                "region": region,
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "comparison_key": base,
                "instrument_id": f"{venue}:{symbol}",
                "route_id": f"{venue.lower()}_spot_public",
                "data_status": data_status,
                "http_status": "200" if data_status == "reachable" else "HTTP 403: Forbidden",
                "latency_ms": 1.0,
                "last_checked_at": "2026-06-19T00:00:00+00:00",
                "bid": bid,
                "ask": ask,
                "last": last,
                "mark_price": None,
                "index_price": None,
                "funding_rate": None,
                "next_funding_time": None,
                "quote_volume_24h": quote_volume,
                "spread_bps": None,
                "source_url": "https://example.test",
                "notes": [],
            }
        )

    def _quality_obs(
        self,
        venue: str,
        symbol: str,
        base: str,
        quote: str,
        last: float,
        quote_volume: float,
        quality_score: float,
        region: str | None = None,
    ) -> dict:
        row = self._obs(venue, symbol, base, quote, last, quote_volume, region=region)
        row.update(
            {
                "quality_status": "verified",
                "quality_score": quality_score,
                "usd_normalized_last": last if quote in {"USD", "USDT", "USDC"} else None,
                "comparison_price": last if quote in {"USD", "USDT", "USDC"} else row.get("comparison_price"),
                "quote_normalization_status": "usd_like" if quote in {"USD", "USDT", "USDC"} else row.get("quote_normalization_status"),
                "freshness_age_seconds": 5.0,
                "simulated_fills": {
                    "buy": {"1000": {"filled": True, "slippage_bps": 2.0}},
                    "sell": {"1000": {"filled": True, "slippage_bps": 2.0}},
                },
                "anomaly_flags": [],
                "critical_anomaly_flags": [],
            }
        )
        return row


if __name__ == "__main__":
    unittest.main()
