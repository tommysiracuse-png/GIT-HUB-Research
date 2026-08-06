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
from paper_order_router import FRONTIER_PAPER_ADMISSION_REASON_PREFIX
import route_resolver
import signal_redesign
from paper_exploration import fair_lineage_order
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
    def test_okx_basis_mean_reversion_variant_stays_shadow_only_not_route_rejected(self) -> None:
        guarded = frontier._paper_only_apply_okx_basis_variant_gate(
            {
                "market_key": "OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
                "venue": "OKX",
                "variant": "basis_mean_reversion_short_perp",
                "strategy": "perp_funding_basis",
                "instrument_id": "BTC-USDT-SWAP",
                "route_confidence": 0.82,
            }
        )

        self.assertFalse(guarded.get("route_rejected", False))
        self.assertTrue(guarded["paper_only_variant_gate"]["blocked"])
        self.assertTrue(guarded["paper_only_variant_gate"]["shadow_only"])
        self.assertEqual(
            "decayed_basis_mean_reversion_quarantine",
            guarded["paper_only_variant_gate"]["reason"],
        )
        self.assertTrue(guarded["route_shadow_only"])
        self.assertEqual("paper_shadow_only", guarded["route_status"])
        self.assertEqual(
            "decayed_basis_mean_reversion_quarantine",
            guarded["paper_only_shadow_reason"],
        )
        self.assertEqual("market_key", guarded["paper_only_variant_gate"]["match_source"])

    def test_okx_basis_mean_reversion_signal_key_stays_shadow_only_without_variant_field(self) -> None:
        guarded = frontier._paper_only_apply_okx_basis_variant_gate(
            {
                "signal_key": "OKX|perp_funding_basis|basis_mean_reversion_long_perp|conditional",
                "venue": "OKX",
                "strategy": "perp_funding_basis",
                "instrument_id": "BTC-USDT-SWAP",
                "route_confidence": 0.79,
            }
        )

        self.assertFalse(guarded.get("route_rejected", False))
        self.assertTrue(guarded["paper_only_variant_gate"]["blocked"])
        self.assertEqual("basis_mean_reversion_long_perp", guarded["paper_only_variant_gate"]["variant"])
        self.assertEqual("signal_key", guarded["paper_only_variant_gate"]["match_source"])
        self.assertEqual("paper_shadow_only", guarded["route_status"])

    def test_okx_basis_positive_control_variant_remains_routable(self) -> None:
        guarded = frontier._paper_only_apply_okx_basis_variant_gate(
            {
                "market_key": "OKX|perp_funding_basis|funding_capture_long_perp|conditional",
                "venue": "OKX",
                "variant": "funding_capture_long_perp",
                "strategy": "perp_funding_basis",
                "instrument_id": "BTC-USDT-SWAP",
                "route_confidence": 0.91,
            }
        )

        self.assertFalse(guarded.get("route_rejected", False))
        self.assertFalse(guarded["paper_only_variant_gate"]["blocked"])
        self.assertEqual("allowed_okx_basis_variant", guarded["paper_only_variant_gate"]["reason"])
        self.assertNotIn("route_shadow_only", guarded)
        self.assertNotIn("paper_only_shadow_reason", guarded)

    def test_okx_basis_positive_control_signal_key_wins_over_descriptive_text(self) -> None:
        guarded = frontier._paper_only_apply_okx_basis_variant_gate(
            {
                "signal_key": "OKX|perp_funding_basis|funding_capture_short_perp|standard",
                "venue": "OKX",
                "strategy": "perp_funding_basis",
                "title": "basis_mean_reversion_short_perp diagnostic replay",
                "instrument_id": "BTC-USDT-SWAP",
                "route_confidence": 0.88,
            }
        )

        self.assertFalse(guarded.get("route_rejected", False))
        self.assertFalse(guarded["paper_only_variant_gate"]["blocked"])
        self.assertEqual("signal_key", guarded["paper_only_variant_gate"]["match_source"])
        self.assertEqual("allowed_okx_basis_variant", guarded["paper_only_variant_gate"]["reason"])
        self.assertNotIn("route_shadow_only", guarded)

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

    def test_scan_batch_adds_active_program_universe_for_bounded_intraday_coverage(self) -> None:
        cfg = settings()
        cfg["frontier_crypto_adapter"]["intraday_feature_max_observations"] = 1
        cfg["frontier_crypto_adapter"]["intraday_feature_max_per_venue"] = 1
        high = self._obs("OKX_SPOT", "HIGH-USDT", "HIGH", "USDT", 100, 10_000_000)
        low = self._obs("OKX_SPOT", "LOW-USDT", "LOW", "USDT", 100, 10_000)
        registry = {
            "venues": [
                {
                    "venue": "OKX_SPOT",
                    "intraday": {
                        "url_template": "https://public.test/{symbol}",
                        "parser": "okx_1m_candles",
                    },
                }
            ]
        }
        candles = []
        for index in range(62):
            close = 100.5 if index == 61 else 100.0
            volume = 2000.0 if index == 61 else 1000.0
            candles.append([index * 60_000, "0", "0", "0", str(close), "0", "0", str(volume), "1"])
        requirements = {
            "active_program_count": 1,
            "required_features": ["microstructure_history_ready"],
            "programs": [
                {
                    "strategy_lab_id": "coverage_test_v1",
                    "required_features": ["microstructure_history_ready"],
                    "universe": {
                        "asset_classes": ["crypto_spot"],
                        "trade_types": ["frontier_crypto_venue_map"],
                        "market_types": ["spot"],
                        "quotes": ["USDT"],
                        "bases": ["LOW"],
                    },
                }
            ],
        }

        with (
            mock.patch.object(frontier, "load_venue_registry", return_value=registry),
            mock.patch.object(frontier, "scan_venues", return_value=[high, low]),
            mock.patch.object(frontier, "_select_observations", return_value=[high]),
            mock.patch.object(frontier, "_active_strategy_intraday_requirements", return_value=requirements),
            mock.patch.object(frontier, "_reference_prices", return_value={}),
            mock.patch.object(
                frontier,
                "fetch_json",
                return_value={"ok": True, "payload": {"data": list(reversed(candles))}},
            ) as fetch,
        ):
            batch = frontier.build_scan_batch(
                cfg,
                conn=object(),
                write_preliminary_report=False,
            )

        self.assertEqual(1, fetch.call_count)
        self.assertEqual(1, batch.metadata["intraday_features"]["strategy_required_eligible_count"])
        self.assertEqual(1, batch.metadata["intraday_features"]["strategy_required_selected_count"])
        low_frame = next(row for row in batch.observations if row["inst_id"] == low["instrument_id"])
        self.assertEqual("crypto_spot", low_frame["asset_class"])
        self.assertEqual("frontier_crypto_venue_map", low_frame["trade_type"])
        self.assertEqual("spot", low_frame["market_type"])
        self.assertEqual("USDT", low_frame["quote"])
        self.assertEqual("LOW", low_frame["base"])
        self.assertEqual(1.0, low_frame["microstructure_history_ready"])
        self.assertGreater(low_frame["return_1m_bps"], 0.0)
        self.assertGreater(low_frame["relative_volume_1m_60m"], 1.0)

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

    def test_valr_public_parser_carries_native_spot_metadata_to_paper_candidates(self) -> None:
        target = {
            "venue": "VALR",
            "market_type": "spot",
            "route_id": "valr_spot_public",
            "url": "https://api.valr.com/v1/public/marketsummary",
            "region": "Africa",
        }
        rows = frontier._parse_valr_market_summary(
            target,
            result(
                [
                    {
                        "currencyPair": "BTCZAR",
                        "bidPrice": "1790000",
                        "askPrice": "1791000",
                        "lastTradedPrice": "1790500",
                        "baseVolume": "20",
                        "lastTradedTimestamp": "2026-08-05T17:00:00Z",
                    }
                ]
            ),
        )

        self.assertEqual(1, len(rows))
        valr = rows[0]
        self.assertEqual((valr["base"], valr["quote"]), ("BTC", "ZAR"))
        self.assertEqual(valr["best_bid"], 1790000.0)
        self.assertEqual(valr["best_ask"], 1791000.0)
        self.assertEqual(valr["last_trade_timestamp"], "2026-08-05T17:00:00+00:00")
        self.assertEqual(valr["instrument_metadata"]["venue_symbol"], "BTCZAR")
        self.assertEqual(valr["market_data_origin"], "native_public_spot")

        valr.update(
            {
                "usd_normalized_last": 99_500.0,
                "canonical_normalized_price": 99_500.0,
                "comparison_price": 99_500.0,
                "quote_normalization_status": "same_venue_stablecoin_reference",
                "local_quote_observe_only": False,
                "quality_status": "verified",
                "quality_score": 85.0,
                "freshness_age_seconds": 1.0,
                "anomaly_flags": [],
                "critical_anomaly_flags": [],
            }
        )
        candidate = frontier._candidate_from_observation(
            valr,
            settings(),
            100_000.0,
            2,
            reference_observations=[valr],
        )

        self.assertEqual(candidate["direction"], "long_frontier_spot")
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertIsNone(candidate["candidate_reject_reason"])
        self.assertEqual(candidate["market_data_origin"], "native_public_spot")
        self.assertEqual(candidate["instrument_metadata"]["venue"], "VALR")
        self.assertEqual(candidate["venue_constraints"]["price_precision"], 0)
        self.assertEqual(candidate["instrument_metadata"]["venue_constraints"]["constraint_source"], "observed_public_payload")

        short_valr = copy.deepcopy(valr)
        short_valr.update(
            {
                "usd_normalized_last": 100_500.0,
                "canonical_normalized_price": 100_500.0,
                "comparison_price": 100_500.0,
            }
        )
        short_candidate = frontier._candidate_from_observation(
            short_valr,
            settings(),
            100_000.0,
            2,
            reference_observations=[short_valr],
        )
        self.assertEqual(short_candidate["direction"], "short_frontier_spot")
        self.assertFalse(short_candidate["paper_entry_blocked"])

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
                "venue_health_score": 90.0,
                "venue_health": {"venue_quality_score": 90.0},
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
        self.assertEqual(luno["native_quote_currency"], "ZAR")
        self.assertEqual(luno["canonical_quote_currency"], "USD")
        self.assertAlmostEqual(luno["canonical_normalized_price"], 100_000.0)
        self.assertEqual(luno["fx_source"], "ExchangeRate-API Open:USD/ZAR")
        self.assertEqual(luno["fx_age_seconds"], 10.0)
        self.assertIsNone(luno["suppression_reason"])
        self.assertTrue(luno["product_metadata_validated"])
        self.assertTrue(luno["conversion_path_validated"])
        self.assertEqual(luno["quote_normalization_status"], "external_fx_reference")
        self.assertEqual(candidate["regional_candidate_gate_status"], "insufficient_verified_depth_snapshots")
        self.assertIn("insufficient_verified_depth_snapshots", candidate["local_quote_diagnostics"])
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertEqual(ready["regional_candidate_gate_status"], "passed")
        self.assertFalse(ready["paper_entry_blocked"])

    def test_local_fiat_telemetry_is_normalized_and_only_downranks_paper_candidate(self) -> None:
        cfg = settings()
        local = self._obs("LUNO", "XBTZAR", "BTC", "ZAR", 1_818_000.0, 1_800_000, region="africa")
        peer = self._obs("COINBASE", "BTC-USD", "BTC", "USD", 100_000.0, 10_000_000)
        normalized = frontier._annotate_local_quote_premiums(
            frontier._normalize_regional_quotes(
                [local, peer],
                fx_references={
                    "ZAR": {
                        "rate": 18.0,
                        "provider": "public_fx",
                        "age_seconds": 10.0,
                        "source_url": "https://example.test/fx",
                    }
                },
            )
        )
        luno = next(row for row in normalized if row["venue"] == "LUNO")
        luno.update(
            {
                "quality_status": "verified",
                "quality_score": 90.0,
                "venue_health_score": 90.0,
                "venue_health": {"venue_quality_score": 90.0},
                "verified_depth_snapshot_count": 3,
                "simulated_fills": {
                    "buy": {"1000": {"filled": True, "slippage_bps": 1.0}},
                    "sell": {"1000": {"filled": True, "slippage_bps": 1.0}},
                },
                "anomaly_flags": [],
                "critical_anomaly_flags": [],
            }
        )

        candidate = frontier._candidate_from_observation(luno, cfg, 100_000.0, 2, reference_observations=normalized)

        self.assertEqual(luno["quote_ccy"], "ZAR")
        self.assertTrue(luno["local_quote_flag"])
        self.assertAlmostEqual(luno["fx_to_usd"], 1.0 / 18.0)
        self.assertAlmostEqual(luno["fx_age_minutes"], 10.0 / 60.0, places=3)
        self.assertAlmostEqual(luno["native_quote_volume_24h"], 1_800_000.0)
        self.assertAlmostEqual(luno["normalized_quote_volume_usd"], 100_000.0)
        self.assertAlmostEqual(luno["normalized_mid_usd"], 101_000.0)
        self.assertAlmostEqual(luno["premium_vs_reference_bps"], 100.0)
        self.assertAlmostEqual(candidate["native_quote_volume_24h"], 1_800_000.0)
        self.assertAlmostEqual(candidate["normalized_quote_volume_usd"], 100_000.0)
        self.assertEqual(candidate["premium_vs_reference_bps"], 100.0)
        self.assertGreater(candidate["local_quote_score_penalty"], 0.0)
        self.assertFalse(candidate["paper_entry_blocked"])

    def test_stale_external_fx_stays_priceable_but_is_excluded_from_active_ranking(self) -> None:
        cfg = settings()
        local = self._obs("LUNO", "XBTZAR", "BTC", "ZAR", 1_782_000.0, 180_000_000, region="Africa")
        peer = self._obs("COINBASE", "BTC-USD", "BTC", "USD", 100_000.0, 10_000_000)

        normalized = frontier._normalize_regional_quotes(
            [local, peer],
            fx_references={
                "ZAR": {
                    "rate": 18.0,
                    "provider": "public_fx",
                    "age_seconds": 61.0,
                    "source_url": "https://example.test/fx",
                }
            },
            policy={
                "regional_fx_normalization_enabled": True,
                "regional_fx_require_fresh_reference": True,
                "regional_fx_max_age_seconds": 60.0,
            },
        )
        luno = next(row for row in normalized if row["venue"] == "LUNO")
        luno.update(
            {
                "quality_status": "verified",
                "quality_score": 90.0,
                "venue_health_score": 90.0,
                "venue_health": {"venue_quality_score": 90.0},
                "verified_depth_snapshot_count": 3,
                "simulated_fills": {
                    "buy": {"1000": {"filled": True, "slippage_bps": 1.0}},
                    "sell": {"1000": {"filled": True, "slippage_bps": 1.0}},
                },
                "anomaly_flags": [],
                "critical_anomaly_flags": [],
            }
        )
        candidate = frontier._candidate_from_observation(
            luno,
            cfg,
            100_000.0,
            2,
            reference_observations=normalized,
        )
        ranked = frontier.rank_frontier_paper_candidates([candidate], cfg)

        self.assertEqual(luno["quote_normalization_status"], "stale_fx_reference")
        self.assertEqual(luno["suppression_reason"], "stale_fx_reference")
        self.assertEqual(luno["fx_age_seconds"], 61.0)
        self.assertAlmostEqual(luno["canonical_normalized_price"], 99_000.0)
        self.assertEqual(frontier._comparison_price(luno), 99_000.0)
        self.assertNotIn("BTC", frontier._reference_prices(normalized, cfg))
        self.assertEqual(candidate["direction"], "long_frontier_spot")
        self.assertEqual(candidate["suppression_reason"], "stale_fx_reference")
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertFalse(candidate["quote_ranking_eligible"])
        self.assertFalse(ranked[0]["paper_active_scoring_eligible"])
        self.assertEqual("stale_fx_reference", ranked[0]["paper_fx_ranking_reason"])
        self.assertEqual("stale_fx_reference_shadow_only", ranked[0]["paper_quality_filter_status"])
        self.assertEqual(0.0, ranked[0]["paper_ranking_score"])

    def test_unmatched_quote_and_invalid_product_metadata_fail_closed(self) -> None:
        unmatched = self._obs("LOCAL", "BTC-XYZ", "BTC", "XYZ", 100_000.0, 1_000_000)
        invalid = self._obs("LOCAL", "BROKEN", "", "ZAR", 1_800_000.0, 1_000_000)

        unmatched_row, invalid_row = frontier._normalize_regional_quotes([unmatched, invalid])

        self.assertEqual(unmatched_row["suppression_reason"], "unmatched_quote_currency")
        self.assertEqual(unmatched_row["quote_normalization_status"], "unsupported_quote")
        self.assertEqual(frontier._comparison_price(unmatched_row), 0.0)
        self.assertEqual(invalid_row["suppression_reason"], "invalid_product_metadata")
        self.assertFalse(invalid_row["product_metadata_validated"])
        self.assertTrue(invalid_row["local_quote_observe_only"])

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
            self._obs("B", "ABC-USDT", "ABC", "USDT", 98, 100000),
            self._obs("C", "ABC-USDT", "ABC", "USDT", 102, 100000),
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

    def test_candidate_preserves_ready_local_trend_for_cross_surface_confirmation(self) -> None:
        cfg = settings()
        ready_observation = self._obs("OKX", "ABC-USDT", "ABC", "USDT", 90, 100000)
        ready_observation.update(
            {
                "return_1m_bps": 6.25,
                "microstructure_history_ready": 1.0,
                "microstructure_status": "ready",
            }
        )

        ready = frontier._candidate_from_observation(ready_observation, cfg, 100, 3)
        unavailable = frontier._candidate_from_observation(
            {
                **ready_observation,
                "return_1m_bps": -9.0,
                "microstructure_history_ready": 0.0,
                "microstructure_status": "insufficient_closed_candles",
            },
            cfg,
            100,
            3,
        )

        self.assertEqual(6.25, ready["local_short_horizon_trend_bps"])
        self.assertTrue(ready["local_short_horizon_trend_ready"])
        self.assertEqual("1m", ready["local_short_horizon_trend_window"])
        self.assertIsNone(unavailable["local_short_horizon_trend_bps"])
        self.assertFalse(unavailable["local_short_horizon_trend_ready"])

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
            self._obs("B", "ABC-USDT", "ABC", "USDT", 98, 100000),
            self._obs("C", "ABC-USDT", "ABC", "USDT", 102, 100000),
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
        self.assertTrue(
            {
                "gross_edge_bps",
                "modeled_cost_bps",
                "net_edge_bps",
                "freshness_minutes",
                "gating_reason",
                "route_health_confirmed",
                "simulated_order_allocation",
            }.issubset(summary["top_dislocations"][0])
        )
        self.assertIn("spot_borrow", summary["by_route_blocker"])
        self.assertIn("candidate_activity", summary)
        self.assertIn("marketability_confirmed_route_candidates", summary["candidate_activity"])
        self.assertIn("marketability_conservative_route_candidates", summary["candidate_activity"])
        self.assertIn("dislocation_quality_score", summary)
        self.assertIn("dislocation_quality_diagnostics", summary)
        self.assertIn("dislocation_quality_cohort_outcomes", summary)
        self.assertIn("short_cost_decomposition", summary)
        self.assertIn("dislocation_quality_score", summary["top_dislocations"][0])
        self.assertIn("short_cost_decomposition", summary["top_dislocations"][0])
        self.assertEqual(summary["expansion_map"]["worker_count"], 16)
        self.assertEqual(summary["expansion_map"]["selection_limits"]["max_symbols_per_cycle"], 300)
        self.assertEqual(summary["expansion_map"]["venue_quota_report"]["A"]["status"], "partial")

    def test_dislocation_quality_ranks_broad_stable_references_without_blocking_fragile_paper(self) -> None:
        cfg = settings()
        stable_local = self._quality_obs("LOCAL_STABLE", "ABC-USDT", "ABC", "USDT", 98.0, 100_000, quality_score=85)
        stable_peers = [
            self._quality_obs("PEER_A", "ABC-USDT", "ABC", "USDT", 100.0, 100_000, quality_score=85),
            self._quality_obs("PEER_B", "ABC-USDT", "ABC", "USDT", 100.1, 100_000, quality_score=85),
            self._quality_obs("PEER_C", "ABC-USDT", "ABC", "USDT", 99.9, 100_000, quality_score=85),
        ]
        fragile_local = self._quality_obs("LOCAL_FRAGILE", "XYZ-USDT", "XYZ", "USDT", 98.0, 100_000, quality_score=85)
        fragile_peer = self._quality_obs("PEER_D", "XYZ-USDT", "XYZ", "USDT", 100.0, 100_000, quality_score=85)

        stable = frontier._candidate_from_observation(
            stable_local, cfg, 100.0, 4, reference_observations=[stable_local, *stable_peers]
        )
        fragile = frontier._candidate_from_observation(
            fragile_local, cfg, 100.0, 2, reference_observations=[fragile_local, fragile_peer]
        )
        ranked = frontier.rank_frontier_paper_candidates([fragile, stable], cfg)

        self.assertGreater(stable["dislocation_quality_score"], fragile["dislocation_quality_score"])
        self.assertIn("narrow_reference_set", fragile["dislocation_quality_diagnostics"])
        self.assertFalse(fragile["paper_entry_blocked"])
        self.assertEqual("ranked_not_blocked", fragile["paper_quality_filter_status"])
        self.assertEqual(stable["inst_id"], ranked[0]["inst_id"])
        self.assertEqual(1, ranked[0]["paper_quality_rank"])

        ordered = fair_lineage_order(
            [
                {**fragile, "venue": "SAME", "paper_ranking_score": 55.0, "score": 95.0},
                {**stable, "venue": "SAME", "paper_ranking_score": 75.0, "score": 45.0},
            ],
            0,
            cfg,
        )
        self.assertEqual(stable["inst_id"], ordered[0]["inst_id"])

    def test_dislocation_quality_prioritizes_lineages_without_suppressing_diagnostics(self) -> None:
        cfg = settings()
        stable = {
            "inst_id": "STABLE-USDT",
            "venue": "STABLE",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 45.0,
            "paper_ranking_score": 80.0,
            "paper_entry_blocked": False,
        }
        fragile = {
            "inst_id": "FRAGILE-USDT",
            "venue": "FRAGILE",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 90.0,
            "paper_ranking_score": 35.0,
            "paper_entry_blocked": False,
        }

        ordered = fair_lineage_order([fragile, stable], 0, cfg)

        self.assertEqual("STABLE-USDT", ordered[0]["inst_id"])
        self.assertEqual({"STABLE-USDT", "FRAGILE-USDT"}, {row["inst_id"] for row in ordered})
        self.assertFalse(fragile["paper_entry_blocked"])

    def test_route_resolver_compatibility_for_frontier_candidate(self) -> None:
        cfg = settings()
        observation = self._obs("C", "ABC-USDT", "ABC", "USDT", 102, 100000)
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
            self._quality_obs("B", "ABC-USDT", "ABC", "USDT", 98, 20_000_000, quality_score=90),
            self._quality_obs("C", "ABC-USDT", "ABC", "USDT", 102, 20_000_000, quality_score=90),
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
            self._quality_obs("LUNO", "BTC-USDT", "BTC", "USDT", 102_000, 20_000_000, quality_score=90, region="Africa"),
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
            self._quality_obs("BYBIT_SPOT", "ABCUSDT", "ABC", "USDT", 99, 20_000_000, quality_score=80),
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

    def test_marketability_gate_admits_fresh_deep_confirmed_paper_route(self) -> None:
        cfg = settings()
        local = self._quality_obs("FRONTIER", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        self.assertEqual("short_frontier_spot", candidate["direction"])
        self.assertEqual("passed", candidate["marketability_gate_status"])
        self.assertTrue(all(check["passed"] for check in candidate["marketability_gate"]["checks"].values()))
        self.assertTrue(candidate["route_health_confirmation"]["confirmed"])
        self.assertEqual("primary", candidate["simulated_order_allocation"]["mode"])
        self.assertGreater(candidate["paper_allocation_multiplier"], 0.0)
        self.assertLess(candidate["paper_allocation_multiplier"], 1.0)

    def test_frontier_short_route_report_refreshes_before_sizing_for_mexc_and_valr(self) -> None:
        cfg = settings()
        for venue in ("MEXC", "VALR"):
            with self.subTest(venue=venue):
                local = self._quality_obs(venue, "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
                peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.0, 100_000, quality_score=85)
                seen_before_sizing = []

                def assert_report_before_sizing(candidate, observation, runtime_settings):
                    report = candidate["frontier_short_spot_route_requirements_report"]
                    seen_before_sizing.append(report)
                    self.assertTrue(candidate["route_requirements_prepared_before_ranking_and_sizing"])
                    self.assertTrue(report["paper_only"])
                    self.assertTrue(report["prepared_before_ranking_and_sizing"])
                    self.assertEqual("unsupported", report["per_venue_status"]["status"])
                    self.assertEqual("unavailable", report["borrow_availability"])
                    self.assertEqual("unsupported", report["margin_eligibility"]["mode"])
                    self.assertEqual("public_data_only", report["api_connectivity"]["status"])
                    self.assertEqual("unconfirmed", report["api_connectivity"]["path_readiness"])
                    self.assertEqual("observed", report["route_freshness"]["status"])
                    self.assertIn("maker_fee_bps", report["fee_tiers"])
                    self.assertFalse(report["entry_blocked"])
                    return candidate

                with mock.patch.object(
                    frontier,
                    "_apply_frontier_effective_edge",
                    side_effect=assert_report_before_sizing,
                ):
                    candidate = frontier._candidate_from_observation(
                        local,
                        cfg,
                        100.0,
                        2,
                        reference_observations=[local, peer],
                    )

                self.assertEqual(1, len(seen_before_sizing))
                self.assertFalse(candidate["paper_entry_blocked"])

    def test_ranking_refreshes_prebuilt_frontier_short_route_reports(self) -> None:
        candidate = {
            "venue": "MEXC",
            "inst_id": "MEXC:ABC-USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 55.0,
            "estimated_fee_bps_per_side": 10.0,
            "freshness_age_seconds": 2.0,
            "latency_ms": 10.0,
        }

        ranked = frontier.rank_frontier_paper_candidates([candidate], settings())

        report = ranked[0]["frontier_short_spot_route_requirements_report"]
        self.assertTrue(ranked[0]["route_requirements_prepared_before_ranking_and_sizing"])
        self.assertEqual("MEXC", report["candidate"]["venue"])
        self.assertEqual("unsupported", report["per_venue_status"]["status"])
        self.assertFalse(report["entry_blocked"])

    def test_ranking_shadow_labels_exact_unsupported_conditional_short_without_blocking_emission(self) -> None:
        candidate = {
            "venue": "MEXC",
            "inst_id": "MEXC:EDGE-USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 70.0,
            "dislocation_quality_score": 65.0,
            "effective_edge_bps": 18.0,
            "estimated_fee_bps_per_side": 10.0,
            "gross_edge_bps_estimate": 32.0,
            "estimated_round_trip_cost_bps": 20.0,
            "freshness_age_seconds": 2.0,
            "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
            "quality_action": "normal",
            "anomaly_flags": [],
        }

        ranked = frontier.rank_frontier_paper_candidates([candidate], settings())

        self.assertEqual([candidate], ranked)
        self.assertFalse(candidate.get("paper_entry_blocked", False))
        self.assertFalse(candidate["paper_active_scoring_eligible"])
        self.assertTrue(candidate["paper_route_feasibility_shadow_label"])
        self.assertEqual(
            "conditional_short_paper_metadata_unsupported",
            candidate["route_feasibility_reason"],
        )
        self.assertEqual(
            "route_feasibility_shadow_only",
            candidate["paper_quality_filter_status"],
        )
        self.assertLess(candidate["paper_ranking_score"], 20.0)
        self.assertTrue(candidate["paper_ineligible"])
        self.assertIn("borrow_availability_model_unsupported", candidate["paper_route_rationale_codes"])
        report = candidate["frontier_short_spot_route_requirements_report"]
        self.assertEqual(
            "conditional_short_paper_metadata_unsupported",
            report["route_feasibility_reason"],
        )
        summary = frontier.summarize([], ranked)
        self.assertEqual(0, summary["active_paper_review_candidate_count"])
        self.assertEqual(1, summary["shadow_or_observe_only_candidate_count"])
        self.assertEqual(
            1,
            summary["candidate_activity"]["route_feasibility_shadow_candidates"],
        )

    def test_ranking_shadow_labels_unknown_conditional_short_without_verified_route(self) -> None:
        candidate = {
            "venue": "BITSO",
            "inst_id": "BITSO:EDGE-USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 70.0,
            "dislocation_quality_score": 65.0,
            "effective_edge_bps": 18.0,
            "estimated_fee_bps_per_side": 10.0,
            "gross_edge_bps_estimate": 32.0,
            "estimated_round_trip_cost_bps": 20.0,
            "freshness_age_seconds": 2.0,
            "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
            "quality_action": "normal",
            "anomaly_flags": [],
        }

        ranked = frontier.rank_frontier_paper_candidates([candidate], settings())

        self.assertEqual([candidate], ranked)
        self.assertFalse(candidate.get("paper_entry_blocked", False))
        self.assertFalse(candidate["paper_active_scoring_eligible"])
        self.assertTrue(candidate["paper_route_feasibility_shadow_label"])
        self.assertEqual(
            "conditional_short_paper_metadata_unsupported",
            candidate["route_feasibility_reason"],
        )
        self.assertEqual(
            "route_feasibility_shadow_only",
            candidate["paper_quality_filter_status"],
        )
        self.assertLess(candidate["paper_ranking_score"], 60.0)
        self.assertTrue(candidate["paper_ineligible"])
        self.assertIn("borrow_availability_model_unsupported", candidate["paper_route_rationale_codes"])
        report = candidate["frontier_short_spot_route_requirements_report"]
        self.assertEqual(
            "conditional_short_paper_metadata_unsupported",
            report["route_feasibility_reason"],
        )
        summary = frontier.summarize([], ranked)
        self.assertEqual(0, summary["active_paper_review_candidate_count"])
        self.assertEqual(1, summary["shadow_or_observe_only_candidate_count"])
        self.assertEqual(
            1,
            summary["candidate_activity"]["route_feasibility_shadow_candidates"],
        )

    def test_short_cost_decomposition_prices_short_friction_without_blocking_paper(self) -> None:
        cfg = settings()
        local = self._quality_obs("FRONTIER", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )
        frontier.rank_frontier_paper_candidates([candidate], cfg)
        costs = candidate["short_cost_decomposition"]
        expected_net = (
            costs["gross_edge_bps"]
            - costs["spread_cost_bps"]
            - costs["depth_slippage_cost_bps"]
            - costs["estimated_taker_fee_cost_bps"]
            - costs["venue_quality_penalty_bps"]
            - costs["synthetic_route_penalty_bps"]
            - costs["borrow_proxy_penalty_bps"]
        )
        self.assertTrue(costs["applicable"])
        self.assertAlmostEqual(expected_net, costs["net_edge_bps"], places=5)
        self.assertEqual(6.0, costs["borrow_proxy_penalty_bps"])
        self.assertEqual("unconfirmed_borrow_proxy", costs["borrow_proxy_source"])
        self.assertEqual("short_cost_decomposition.net_edge_bps", candidate["paper_ranking_edge_source"])
        self.assertTrue(candidate["paper_entry_blocked"])
        self.assertEqual(
            f"{FRONTIER_PAPER_ADMISSION_REASON_PREFIX}:route_feasibility",
            candidate["candidate_reject_reason"],
        )
        self.assertFalse(candidate["frontier_paper_admission"]["admitted"])

    def test_summary_breaks_out_route_feasibility_reasons_and_verified_exceptions(self) -> None:
        gated = {
            "inst_id": "BITSO:EDGE-USDT",
            "venue": "BITSO",
            "direction": "short_frontier_spot",
            "paper_active_scoring_eligible": False,
            "paper_route_feasibility_shadow_label": True,
            "route_feasibility_reason": "conditional_short_paper_metadata_missing",
            "candidate_reject_reason": "conditional_short_paper_metadata_missing",
        }
        verified = {
            "inst_id": "GATE:EDGE-USDT",
            "venue": "GATE",
            "direction": "short_frontier_spot",
            "paper_active_scoring_eligible": True,
            "paper_route_feasibility_shadow_label": False,
            "route_feasibility_reason": "verified_standard_short_route",
        }

        summary = frontier.summarize([], [gated, verified])

        self.assertEqual(
            1,
            summary["candidate_activity"]["route_feasibility_reason_counts"][
                "conditional_short_paper_metadata_missing"
            ],
        )
        self.assertEqual(
            1,
            summary["candidate_activity"]["route_feasibility_reason_counts"][
                "verified_standard_short_route"
            ],
        )
        self.assertEqual(
            1,
            summary["candidate_activity"]["route_feasibility_verified_exception_candidates"],
        )
        self.assertEqual(
            1,
            summary["paper_short_route_gate"]["status_counts"]["allowed_verified_exception"],
        )
        self.assertEqual(
            1,
            summary["paper_short_route_gate"]["status_counts"]["shadow_only_route_feasibility"],
        )

    def test_short_cost_decomposition_reorders_synthetic_route_by_net_edge(self) -> None:
        cfg = settings()
        direct = {
            "inst_id": "DIRECT",
            "score": 20.0,
            "dislocation_quality_score": 50.0,
            "effective_edge_bps": 20.0,
            "short_cost_decomposition": {"applicable": True, "net_edge_bps": 12.0},
        }
        synthetic = {
            "inst_id": "SYNTHETIC",
            "score": 99.0,
            "dislocation_quality_score": 50.0,
            "effective_edge_bps": 30.0,
            "short_cost_decomposition": {"applicable": True, "net_edge_bps": 5.0},
        }

        ranked = frontier.rank_frontier_paper_candidates([synthetic, direct], cfg)

        self.assertEqual("DIRECT", ranked[0]["inst_id"])
        self.assertEqual("short_cost_decomposition.net_edge_bps", synthetic["paper_ranking_edge_source"])

    def test_effective_edge_decomposes_execution_costs_and_scales_synthetic_routes(self) -> None:
        cfg = settings()
        local = self._quality_obs("FRONTIER", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        local["route_id"] = "synthetic_frontier_paper"

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        model = candidate["effective_edge_model"]
        expected = (
            model["raw_edge_bps"]
            - model["half_spread_bps"]
            - model["estimated_taker_fee_bps"]
            - model["quote_conversion_cost_bps"]
            - model["proxy_drag_bps"]
            - model["freshness_penalty_bps"]
            - model["venue_reliability_penalty_bps"]
        )
        self.assertAlmostEqual(expected, candidate["effective_edge_bps"], places=5)
        self.assertTrue(model["primary_admitted"])
        self.assertTrue(model["synthetic_or_proxy_route"])
        self.assertEqual(0.25, model["primary_allocation_multiplier"])
        self.assertEqual(0.25, candidate["paper_allocation_multiplier"])

    def test_non_positive_effective_edge_uses_counterfactual_route_without_suppressing_candidate(self) -> None:
        cfg = settings()
        cfg["frontier_crypto_adapter"]["min_dislocation_bps"] = 1.0
        local = self._quality_obs("FRONTIER", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.9, 1_000_000, quality_score=90)

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.9,
            2,
            reference_observations=[local, peer],
        )

        self.assertEqual("short_frontier_spot", candidate["direction"])
        self.assertLessEqual(candidate["effective_edge_bps"], 0.0)
        self.assertFalse(candidate["effective_edge_primary_admitted"])
        self.assertIn("effective_edge_not_positive", candidate["effective_edge_admission_reasons"])
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertEqual("counterfactual_guard_value", candidate["simulated_order_allocation"]["mode"])
        self.assertEqual(0.25, candidate["paper_allocation_multiplier"])

    def test_effective_edge_replaces_raw_score_for_frontier_ranking(self) -> None:
        cfg = settings()
        raw_spread_leader = {
            "inst_id": "RAW-LEADER",
            "score": 99.0,
            "dislocation_quality_score": 50.0,
            "effective_edge_bps": 1.0,
        }
        executable_leader = {
            "inst_id": "EXECUTABLE-LEADER",
            "score": 20.0,
            "dislocation_quality_score": 50.0,
            "effective_edge_bps": 12.0,
        }

        ranked = frontier.rank_frontier_paper_candidates([raw_spread_leader, executable_leader], cfg)

        self.assertEqual("EXECUTABLE-LEADER", ranked[0]["inst_id"])

    def test_context_aware_frontier_score_penalizes_weaker_execution_context(self) -> None:
        cfg = settings()
        degraded_context = {
            "inst_id": "DEGRADED-CONTEXT",
            "score": 99.0,
            "dislocation_quality_score": 95.0,
            "effective_edge_bps": 18.0,
            "liquidity_score": 0.5,
            "spread_bps": 11.0,
            "freshness_age_seconds": 80.0,
            "frontier_short_market_context": {"applicable": True, "score": 62.0},
        }
        healthy_context = {
            "inst_id": "HEALTHY-CONTEXT",
            "score": 40.0,
            "dislocation_quality_score": 88.0,
            "effective_edge_bps": 14.0,
            "liquidity_score": 0.92,
            "spread_bps": 2.0,
            "freshness_age_seconds": 4.0,
            "frontier_short_market_context": {"applicable": True, "score": 94.0},
        }

        ranked = frontier.rank_frontier_paper_candidates([degraded_context, healthy_context], cfg)

        self.assertEqual("HEALTHY-CONTEXT", ranked[0]["inst_id"])
        degraded_review = degraded_context["paper_frontier_score"]
        healthy_review = healthy_context["paper_frontier_score"]
        self.assertLess(
            degraded_review["execution_quality_term"] * degraded_review["freshness_term"],
            healthy_review["execution_quality_term"] * healthy_review["freshness_term"],
        )
        self.assertLess(degraded_context["paper_ranking_score"], healthy_context["paper_ranking_score"])

    def test_context_aware_frontier_score_multiplies_edge_by_execution_quality_and_freshness(self) -> None:
        cfg = settings()
        candidate = {
            "inst_id": "BALANCED-CONTEXT",
            "score": 60.0,
            "dislocation_quality_score": 80.0,
            "effective_edge_bps": 16.0,
            "liquidity_score": 0.75,
            "spread_bps": 8.0,
            "freshness_age_seconds": 60.0,
            "frontier_short_market_context": {"applicable": True, "score": 50.0},
        }

        frontier.rank_frontier_paper_candidates([candidate], cfg)

        review = candidate["paper_frontier_score"]
        self.assertEqual(0.86, review["execution_quality_term"])
        self.assertEqual(0.5, review["freshness_term"])
        self.assertEqual(6.88, review["score"])
        self.assertEqual(
            round(
                review["expected_edge_value"]
                * review["execution_quality_term"]
                * review["freshness_term"],
                3,
            ),
            review["score"],
        )

    def test_frontier_score_gate_zeroes_ranking_without_blocking_paper_candidate(self) -> None:
        cfg = settings()
        candidate = {
            "inst_id": "THIN-WIDE",
            "venue": "TEST",
            "direction": "long_frontier_spot",
            "score": 75.0,
            "dislocation_quality_score": 90.0,
            "effective_edge_bps": 20.0,
            "liquidity_score": 0.2,
            "spread_bps": 20.0,
            "freshness_age_seconds": 5.0,
        }

        ranked = frontier.rank_frontier_paper_candidates([candidate], cfg)

        self.assertEqual([candidate], ranked)
        self.assertEqual(0.0, candidate["paper_ranking_score"])
        self.assertFalse(candidate["paper_active_scoring_eligible"])
        self.assertFalse(candidate.get("paper_entry_blocked", False))
        self.assertEqual("frontier_score_gate_shadow_only", candidate["paper_quality_filter_status"])
        self.assertTrue(candidate["paper_frontier_score"]["hard_gate_applied"])
        self.assertEqual(
            ["liquidity_below_minimum", "spread_above_maximum"],
            candidate["paper_frontier_score"]["hard_gate_reasons"],
        )

    def test_quote_context_records_real_book_diagnostics_and_only_penalizes_ranking(self) -> None:
        cfg = settings()
        cfg["frontier_crypto_adapter"]["paper_quote_context"] = {
            "stale_quote_age_ms": 1_000.0,
            "wide_spread_bps": 2.0,
            "shallow_depth_notional": 1_000.0,
            "stale_penalty_points_cap": 8.0,
            "wide_penalty_points_cap": 6.0,
            "shallow_penalty_points_cap": 4.0,
            "synthetic_route_penalty_points": 3.0,
            "total_penalty_points_cap": 10.0,
        }
        candidate = {
            "inst_id": "FRONTIER:ABC-USDT",
            "direction": "long_frontier_spot",
            "spread_bps": 40.0,
            "freshness_age_seconds": 3_600.0,
            "venue_health_score": 80.0,
            "route_id": "synthetic_frontier_proxy",
        }
        observation = {
            "quote_to_usd_multiplier": 1.0,
            "book_levels": {
                "bids": [[100.0, 2.0]],
                "asks": [[101.0, 3.0]],
            },
        }

        annotated = frontier._annotate_frontier_quote_context(candidate, observation, cfg)

        self.assertEqual("long_frontier_spot", annotated["direction"])
        self.assertNotIn("paper_entry_blocked", annotated)
        self.assertEqual(3_600_000.0, annotated["quote_age_ms"])
        self.assertEqual(200.0, annotated["top_of_book_depth_notional"])
        self.assertEqual("two_sided_top_level", annotated["top_of_book_depth_basis"])
        self.assertTrue(annotated["synthetic_route_flag"])
        self.assertEqual("quote_age_exceeds_ranking_threshold", annotated["staleness_reason"])
        self.assertEqual(10.0, annotated["quote_context_ranking_penalty"])
        self.assertEqual("ranked_not_blocked", annotated["frontier_quote_context"]["emission_action"])

        annotated.update({"score": 75.0, "dislocation_quality_score": 50.0, "effective_edge_bps": 20.0})
        ranked = frontier.rank_frontier_paper_candidates([annotated], cfg)
        self.assertEqual("FRONTIER:ABC-USDT", ranked[0]["inst_id"])
        self.assertLess(ranked[0]["paper_ranking_score"], 37.5)

        summary = frontier.summarize([], ranked)
        venue_context = summary["venue_quote_context"]["unknown"]
        self.assertTrue(venue_context["paper_only"])
        self.assertEqual(1, venue_context["candidate_count"])
        self.assertEqual(1, venue_context["synthetic_route_count"])
        self.assertEqual(1, venue_context["stale_candidate_count"])
        self.assertEqual(40.0, venue_context["spread_bps"]["median"])
        self.assertEqual(3_600_000.0, venue_context["quote_age_ms"]["median"])
        self.assertEqual(200.0, venue_context["top_of_book_depth_notional"]["median"])
        self.assertEqual(10.0, venue_context["ranking_penalty_points"]["median"])
        self.assertEqual(40.0, summary["top_dislocations"][0]["spread_bps"])

    def test_marketability_diagnostics_use_conservative_route_for_stale_or_thin_book(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        stale = self._quality_obs("STALE", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        stale["freshness_age_seconds"] = 30.1
        stale_candidate = frontier._candidate_from_observation(
            stale,
            cfg,
            100.5,
            2,
            reference_observations=[stale, peer],
        )

        thin = self._quality_obs("THIN", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        thin["book_levels"] = {
            "bids": [[thin["bid"], 500.0 / thin["bid"]]],
            "asks": [[thin["ask"], 500.0 / thin["ask"]]],
        }
        thin_candidate = frontier._candidate_from_observation(
            thin,
            cfg,
            100.5,
            2,
            reference_observations=[thin, peer],
        )

        self.assertEqual("short_frontier_spot", stale_candidate["direction"])
        self.assertFalse(stale_candidate["paper_entry_blocked"])
        self.assertFalse(stale_candidate["route_health_confirmation"]["confirmed"])
        self.assertEqual("counterfactual_guard_value", stale_candidate["simulated_order_allocation"]["mode"])
        self.assertEqual(0.25, stale_candidate["paper_allocation_multiplier"])
        self.assertIn("top_of_book_depth", thin_candidate["marketability_gate"]["failed_checks"])
        self.assertEqual("short_frontier_spot", thin_candidate["direction"])
        self.assertFalse(thin_candidate["paper_entry_blocked"])
        self.assertEqual("conservative_counterfactual_route", thin_candidate["route_health_confirmation"]["mode"])

    def test_marketability_diagnostics_preserve_bad_print_wide_spread_and_unknown_route(self) -> None:
        cfg = settings()
        cfg["risk"]["max_spread_bps"] = 100.0
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.0, 1_000_000, quality_score=90)

        bad_print = self._quality_obs("BAD_PRINT", "ABC-USDT", "ABC", "USDT", 104.0, 100_000, quality_score=85)
        bad_candidate = frontier._candidate_from_observation(
            bad_print,
            cfg,
            100.0,
            2,
            reference_observations=[bad_print, peer],
        )

        wide = self._quality_obs("WIDE", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        wide["bid"] = 100.8
        wide["ask"] = 101.2
        wide["spread_bps"] = 39.604
        wide_candidate = frontier._candidate_from_observation(
            wide,
            cfg,
            100.0,
            2,
            reference_observations=[wide, peer],
        )

        unknown_route = self._quality_obs("NO_ROUTE", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        unknown_route["route_id"] = ""
        unknown_route.pop("route_mapping_confidence", None)
        route_candidate = frontier._candidate_from_observation(
            unknown_route,
            cfg,
            100.0,
            2,
            reference_observations=[unknown_route, peer],
        )

        self.assertIn("cross_venue_price_confirmation", bad_candidate["marketability_gate"]["failed_checks"])
        self.assertIn("spread_sanity", wide_candidate["marketability_gate"]["failed_checks"])
        self.assertIn("route_confidence", route_candidate["marketability_gate"]["failed_checks"])
        self.assertTrue(all(row["direction"] == "short_frontier_spot" for row in (bad_candidate, wide_candidate, route_candidate)))
        self.assertTrue(all(not row["paper_entry_blocked"] for row in (bad_candidate, wide_candidate, route_candidate)))
        self.assertTrue(
            all(
                row["simulated_order_allocation"]["mode"] == "counterfactual_guard_value"
                for row in (bad_candidate, wide_candidate, route_candidate)
            )
        )

    def test_marketability_gate_thresholds_are_configurable(self) -> None:
        cfg = settings()
        cfg["frontier_crypto_adapter"]["marketability_gates"].update(
            {"max_book_age_seconds": 10.0, "min_top_of_book_notional_usd": 2500.0}
        )
        local = self._quality_obs("FRONTIER", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        local["freshness_age_seconds"] = 11.0

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        self.assertEqual(
            ["book_freshness", "top_of_book_depth"],
            candidate["marketability_gate"]["failed_checks"],
        )

    def test_marketability_diagnostics_downsize_unhealthy_venue_telemetry(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        unhealthy = self._quality_obs("UNHEALTHY", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        unhealthy["venue_health_score"] = 59.0
        unhealthy["venue_health"]["venue_quality_score"] = 59.0

        candidate = frontier._candidate_from_observation(
            unhealthy,
            cfg,
            100.5,
            2,
            reference_observations=[unhealthy, peer],
        )

        check = candidate["marketability_gate"]["checks"]["venue_health_score"]
        self.assertFalse(check["passed"])
        self.assertTrue(check["telemetry_present"])
        self.assertIsNone(candidate["candidate_reject_reason"])
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertEqual(0.25, candidate["paper_allocation_multiplier"])
        self.assertIn("marketability_venue_health_score", candidate["marketability_diagnostic_reasons"])

    def test_short_frontier_paper_candidate_exposes_route_cost_and_shortability_diagnostics(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        local = self._quality_obs("MEXC", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        self.assertEqual("short_frontier_spot", candidate["direction"])
        self.assertEqual("unsupported", candidate["paper_route_registry_status"])
        self.assertIn("spot_borrow", candidate["paper_route_required_permissions"])
        self.assertFalse(candidate["is_shortable_paper"])
        self.assertEqual("unconfirmed_short_route_assumption", candidate["synthetic_short_method"])
        self.assertEqual(["mexc_spot_public", "unconfirmed_short_route_assumption", "paper_cover"], candidate["route_path"])
        self.assertAlmostEqual(candidate["spread_bps"], candidate["best_bid_ask_spread_bps"], places=6)
        self.assertEqual(2000.0, candidate["top_of_book_depth_usd"])
        self.assertEqual(2.0, candidate["estimated_slippage_bps"])
        self.assertEqual(5000.0, candidate["quote_age_ms"])
        self.assertEqual(5000.0, candidate["market_data_freshness_ms"])
        self.assertIsNone(candidate["borrow_proxy_bps_if_applicable"])
        self.assertGreater(candidate["venue_confidence_score"], 0.0)
        self.assertLess(candidate["venue_confidence_score"], 1.0)
        self.assertTrue(candidate["frontier_short_paper_diagnostics"]["paper_only"])

        ranked = frontier.rank_frontier_paper_candidates([candidate], cfg)
        summary = frontier.summarize([], ranked)
        venue_context = summary["venue_quote_context"]["MEXC"]
        self.assertEqual(2000.0, venue_context["top_of_book_depth_usd"]["median"])
        self.assertAlmostEqual(
            candidate["venue_confidence_score"],
            venue_context["venue_confidence_score"]["median"],
            places=3,
        )

    def test_paper_venue_diagnostics_normalize_public_route_observability(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "BTC-USDT", "BTC", "USDT", 100.0, 1_000_000, quality_score=90)
        local = self._quality_obs("VALR", "BTC-ZAR", "BTC", "ZAR", 101.0, 100_000, quality_score=88, region="Africa")
        local.update(
            {
                "last_trade_timestamp": "2026-08-05T17:00:00+00:00",
                "deposit_enabled": True,
                "withdrawal_enabled": False,
                "instrument_metadata": {
                    "venue_symbol": "BTCZAR",
                    "networks": ["erc20", {"chain_id": "tron"}],
                },
                "route_quality": {
                    "venue_spread_baseline_bps": 10.0,
                    "spread_to_baseline_ratio": 2.0,
                    "depth_to_size_ratio": 2.5,
                },
            }
        )

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.0,
            2,
            reference_observations=[local, peer],
        )

        diagnostics = candidate["paper_venue_diagnostics"]
        self.assertTrue(diagnostics["paper_only"])
        self.assertEqual("frontier_paper_venue_diagnostics_v1", diagnostics["diagnostic_version"])
        self.assertEqual("diagnostics_ranking_sizing_only", diagnostics["emission_action"])
        self.assertEqual(100.9899, diagnostics["top_of_book"]["best_bid"])
        self.assertEqual(101.0101, diagnostics["top_of_book"]["best_ask"])
        self.assertEqual("2026-08-05T17:00:00+00:00", diagnostics["top_of_book"]["quote_timestamp"])
        self.assertEqual(5.0, diagnostics["top_of_book"]["quote_age_seconds"])
        self.assertAlmostEqual(candidate["spread_bps"], diagnostics["spread_statistics"]["current_spread_bps"], places=6)
        self.assertEqual(10.0, diagnostics["spread_statistics"]["venue_spread_baseline_bps"])
        self.assertEqual(2.0, diagnostics["spread_statistics"]["spread_to_baseline_ratio"])
        self.assertEqual("baseline_deviation_proxy", diagnostics["spread_statistics"]["spread_volatility_method"])
        self.assertEqual(2000.0, diagnostics["displayed_depth"]["top_of_book_depth_usd"])
        self.assertEqual(2.0, diagnostics["displayed_depth"]["slippage_proxy_bps"])
        self.assertEqual(2.5, diagnostics["displayed_depth"]["depth_to_size_ratio"])
        self.assertEqual("reachable", diagnostics["venue_health"]["source_status"])
        self.assertEqual(88.0, diagnostics["venue_health"]["venue_health_score"])
        self.assertEqual("available", diagnostics["access_status"]["deposit_status"])
        self.assertEqual("unavailable", diagnostics["access_status"]["withdrawal_status"])
        self.assertTrue(diagnostics["access_status"]["publicly_reported"])
        self.assertEqual(["ERC20", "TRON"], diagnostics["network_normalization"]["identifiers"])
        self.assertEqual("BTC-ZAR", diagnostics["symbol_mapping"]["venue_symbol"])
        self.assertEqual(0.8, diagnostics["symbol_mapping"]["mapping_confidence"])
        self.assertEqual("observed_mapping_confidence", diagnostics["symbol_mapping"]["mapping_source"])
        self.assertGreater(diagnostics["venue_confidence_score"], 0.0)
        self.assertNotIn("network_identifiers_missing", diagnostics["missing_data_flags"])
        self.assertNotIn("deposit_status_unknown", diagnostics["missing_data_flags"])

    def test_paper_venue_diagnostics_preserve_missing_public_fields_as_flags(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        local = self._quality_obs("MEXC", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        local.pop("route_mapping_confidence", None)
        local.pop("venue_health_score", None)
        local.pop("venue_health", None)
        local.pop("instrument_metadata", None)
        local.pop("route_quality", None)

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        diagnostics = candidate["paper_venue_diagnostics"]
        self.assertIn("venue_health_missing", diagnostics["missing_data_flags"])
        self.assertIn("deposit_status_unknown", diagnostics["missing_data_flags"])
        self.assertIn("withdrawal_status_unknown", diagnostics["missing_data_flags"])
        self.assertIn("network_identifiers_missing", diagnostics["missing_data_flags"])
        self.assertEqual("ABC-USDT", diagnostics["symbol_mapping"]["venue_symbol"])
        self.assertEqual(1.0, diagnostics["symbol_mapping"]["mapping_confidence"])
        self.assertEqual("parser_symbol_normalization", diagnostics["symbol_mapping"]["mapping_source"])
        self.assertFalse(candidate["paper_entry_blocked"])

    def test_marketability_diagnostics_preserve_exploration_when_venue_telemetry_is_missing(self) -> None:
        cfg = settings()
        peer = self._quality_obs("REFERENCE", "ABC-USDT", "ABC", "USDT", 100.5, 1_000_000, quality_score=90)
        local = self._quality_obs("NO_HEALTH", "ABC-USDT", "ABC", "USDT", 101.0, 100_000, quality_score=85)
        local.pop("venue_health_score")
        local.pop("venue_health")

        candidate = frontier._candidate_from_observation(
            local,
            cfg,
            100.5,
            2,
            reference_observations=[local, peer],
        )

        check = candidate["marketability_gate"]["checks"]["venue_health_score"]
        self.assertFalse(check["telemetry_present"])
        self.assertEqual("short_frontier_spot", candidate["direction"])
        self.assertFalse(candidate["paper_entry_blocked"])
        self.assertFalse(candidate["route_health_confirmation"]["confirmed"])
        self.assertEqual("counterfactual_guard_value", candidate["simulated_order_allocation"]["mode"])

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
                "route_mapping_confidence": 0.8,
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
                "freshness_age_seconds": 5.0,
                "quote_to_usd_multiplier": 1.0,
                "book_levels": {
                    "bids": [[bid, 2000.0 / bid]] if bid else [],
                    "asks": [[ask, 2000.0 / ask]] if ask else [],
                },
                "depth_usd": {
                    "bid": {"5": 2000.0, "10": 2000.0, "25": 2000.0},
                    "ask": {"5": 2000.0, "10": 2000.0, "25": 2000.0},
                },
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
                "venue_health_score": quality_score,
                "venue_health": {
                    "venue": venue,
                    "venue_quality_score": quality_score,
                    "snapshot_count": 4,
                    "spread_stability_score": 1.0,
                },
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
