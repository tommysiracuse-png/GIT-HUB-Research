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
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.secondary_cex_spot_strength import SecondaryCexSpotStrengthAdapter
from scan_batch import ScanBatch
from settings import DEFAULT_SETTINGS


def fetch_result(payload: object) -> dict:
    return {
        "ok": True,
        "data_status": "reachable",
        "status": "reachable",
        "http_status": 200,
        "latency_ms": 12.0,
        "payload": payload,
        "received_at": "2026-08-06T12:00:00+00:00",
    }


def spot_observation(
    venue: str,
    symbol: str,
    last: float,
    *,
    price_above_vwap: float,
    new_high: float,
    momentum_confirmation_count: float,
    relative_volume: float,
) -> dict:
    return {
        "venue": venue,
        "market_type": "spot",
        "region": None,
        "symbol": symbol,
        "base": "AAA",
        "quote": "USDT",
        "comparison_key": "AAA",
        "instrument_id": f"{venue}:{symbol}",
        "inst_id": f"{venue}:{symbol}",
        "route_id": f"{venue.lower()}_spot_public",
        "source_url": f"https://example.test/{venue.lower()}",
        "data_status": "reachable",
        "http_status": "200",
        "latency_ms": 1.0,
        "last_checked_at": "2026-08-06T12:00:00+00:00",
        "observed_at": "2026-08-06T12:00:00+00:00",
        "bid": last - 0.05,
        "ask": last + 0.05,
        "last": last,
        "quote_volume_24h": 500000.0,
        "rolling_24_hour_volume": 500000.0,
        "spread_bps": 5.0,
        "quality_status": "verified",
        "quality_score": 85.0,
        "freshness_age_seconds": 1.0,
        "quote_to_usd_multiplier": 1.0,
        "book_levels": {
            "bids": [[last - 0.05, 25.0]],
            "asks": [[last + 0.05, 25.0]],
        },
        "best_bid": last - 0.05,
        "best_ask": last + 0.05,
        "last_trade_timestamp": "2026-08-06T11:59:30+00:00",
        "rolling_vwap_60m": last - 0.25,
        "vwap_dislocation_bps": 25.0,
        "price_above_rolling_vwap": price_above_vwap,
        "new_high_60m": new_high,
        "momentum_confirmation_count": momentum_confirmation_count,
        "momentum_confirmation_ratio": momentum_confirmation_count / 3.0,
        "return_1m_bps": 18.0 if momentum_confirmation_count else -4.0,
        "quote_volume_1m": 14000.0,
        "relative_volume_1m_60m": relative_volume,
        "microstructure_history_ready": 1.0,
        "microstructure_status": "ready",
        "venue_health_score": 82.0,
        "venue_health": {"venue_quality_score": 82.0},
        "instrument_metadata": {
            "venue": venue,
            "venue_symbol": symbol,
            "base_asset": "AAA",
            "quote_asset": "USDT",
            "market_type": "spot",
            "public_read_only": True,
        },
    }


class SecondaryCexSpotNormalizationTests(unittest.TestCase):
    def test_bitget_and_whitebit_spot_parsers_attach_native_metadata(self) -> None:
        bitget_rows = frontier._parse_bitget_spot_tickers(
            {"venue": "BITGET", "market_type": "spot", "route_id": "bitget_spot_public", "url": "u"},
            fetch_result(
                {
                    "data": [
                        {
                            "symbol": "AAAUSDT_SPBL",
                            "bestBid": "9.99",
                            "bestAsk": "10.01",
                            "close": "10",
                            "baseVol": "1000",
                            "ts": "1786017600000",
                            "listingTime": "1785931200000",
                        }
                    ]
                }
            ),
        )
        whitebit_rows = frontier._parse_whitebit_tickers(
            {
                "venue": "WHITEBIT",
                "market_type": "spot",
                "route_id": "whitebit_spot_public",
                "url": "u",
                "quote_assets": ["USD", "USDT"],
            },
            fetch_result(
                {
                    "AAA_USDT": {
                        "bid": "10.10",
                        "ask": "10.20",
                        "last_price": "10.15",
                        "base_volume": "2500",
                        "timestamp": 1786017600,
                        "listing_timestamp": 1785931200,
                    }
                }
            ),
        )

        self.assertEqual(10000.0, bitget_rows[0]["quote_volume_24h"])
        self.assertEqual(9.99, bitget_rows[0]["best_bid"])
        self.assertEqual("2026-08-06T12:00:00+00:00", bitget_rows[0]["last_trade_timestamp"])
        self.assertEqual("native_public_spot", bitget_rows[0]["market_data_origin"])
        self.assertEqual("2026-08-05T12:00:00+00:00", bitget_rows[0]["instrument_metadata"]["listed_at"])

        self.assertEqual(10.10, whitebit_rows[0]["best_bid"])
        self.assertEqual(25375.0, whitebit_rows[0]["quote_volume_24h"])
        self.assertEqual("2026-08-06T12:00:00+00:00", whitebit_rows[0]["last_trade_timestamp"])
        self.assertEqual("native_public_spot", whitebit_rows[0]["market_data_origin"])
        self.assertEqual("2026-08-05T12:00:00+00:00", whitebit_rows[0]["instrument_metadata"]["listed_at"])

    def test_intraday_features_emit_vwap_and_momentum_signals(self) -> None:
        candles = []
        for minute in range(62):
            close = 100.0 + minute * 0.1
            candles.append([minute * 60000, close, close, close, close, 1.0, minute * 60000 + 59000, 1000.0 + minute])
        features = frontier._intraday_features({"parser": "binance_style_1m_klines"}, fetch_result(candles))
        self.assertEqual(1.0, features["microstructure_history_ready"])
        self.assertGreater(features["rolling_vwap_60m"], 100.0)
        self.assertGreater(features["vwap_dislocation_bps"], 0.0)
        self.assertEqual(1.0, features["price_above_rolling_vwap"])
        self.assertEqual(1.0, features["new_high_60m"])
        self.assertEqual(3.0, features["momentum_confirmation_count"])
        self.assertEqual(1.0, features["momentum_confirmation_ratio"])


class SecondaryCexSpotSnapshotTests(unittest.TestCase):
    def test_build_scan_batch_emits_secondary_cex_snapshot_and_caps_weak_long_entry(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["account_capabilities"] = {"crypto_spot": True, "crypto_derivatives": False, "spot_borrow": False}
        settings["risk"]["paper_notional_usd"] = 1000.0
        observations = [
            spot_observation(
                "BITGET",
                "AAAUSDT_SPBL",
                100.0,
                price_above_vwap=0.0,
                new_high=0.0,
                momentum_confirmation_count=0.0,
                relative_volume=0.4,
            ),
            spot_observation(
                "WHITEBIT",
                "AAA_USDT",
                101.0,
                price_above_vwap=1.0,
                new_high=1.0,
                momentum_confirmation_count=3.0,
                relative_volume=1.8,
            ),
        ]

        with mock.patch.object(frontier, "scan_venues", return_value=observations), mock.patch.object(
            frontier,
            "enrich_intraday_features",
            side_effect=lambda rows, *_args, **_kwargs: (rows, {"enabled": True, "selected_count": len(rows), "ready_count": len(rows)}),
        ):
            batch = frontier.build_scan_batch(settings, write_preliminary_report=False)

        snapshot = batch.metadata["secondary_cex_spot_strength"]
        self.assertEqual(["BITGET", "WHITEBIT", "OKX_SPOT", "GATE", "MEXC", "KUCOIN", "BYBIT_SPOT"], snapshot["target_venues"])
        self.assertEqual(2, snapshot["priceable_symbol_count"])
        self.assertGreater(snapshot["pct_symbols_above_rolling_vwap"], 0.0)
        self.assertEqual(0, snapshot["confirmed_long_candidate_count"])

        bitget_long = next(row for row in batch.candidates if row["venue"] == "BITGET")
        self.assertEqual("long_frontier_spot", bitget_long["direction"])
        self.assertTrue(bitget_long["paper_entry_confirmation_capped"])
        self.assertIn(
            bitget_long["paper_entry_confirmation_reason"],
            {"confidence_below_threshold", "trend_confirmation_missing", "venue_strength_below_threshold"},
        )
        self.assertLessEqual(bitget_long["paper_allocation_multiplier"], 0.25)
        self.assertEqual("capped_for_counterfactual_entry", bitget_long["paper_entry_confirmation_status"])
        self.assertIn("listing_age_days", bitget_long)
        self.assertIn("cross_venue_dislocation_bps", bitget_long)


class SecondaryCexSpotAdapterTests(unittest.TestCase):
    def test_adapter_is_registered_and_reuses_frontier_snapshot(self) -> None:
        adapter_id = "secondary_cex_spot_strength_public"
        self.assertIn(adapter_id, discover_adapters())
        self.assertIsInstance(get_adapter(adapter_id), SecondaryCexSpotStrengthAdapter)

        batch = ScanBatch(
            source="Frontier crypto public REST",
            candidates=[{"venue": "BITGET", "market_type": "spot", "direction": "long_frontier_spot"}],
            observations=[],
            metadata={
                "selected_observations": [
                    {
                        "venue": "BITGET",
                        "market_type": "spot",
                        "inst_id": "BITGET:AAAUSDT_SPBL",
                        "instrument_id": "BITGET:AAAUSDT_SPBL",
                        "symbol": "AAAUSDT_SPBL",
                        "last": 100.0,
                        "data_status": "reachable",
                        "price_above_rolling_vwap": 1.0,
                        "new_high_60m": 1.0,
                        "momentum_confirmation_count": 3.0,
                        "relative_volume_1m_60m": 1.5,
                        "listing_age_days": 2.0,
                        "cross_venue_dislocation_bps": -35.0,
                    }
                ]
            },
        )
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        with mock.patch("adapters.venues.secondary_cex_spot_strength.frontier.build_scan_batch", return_value=batch):
            adapter_batch = SecondaryCexSpotStrengthAdapter().scan(settings)

        self.assertEqual("reachable", adapter_batch.metadata["source_status"])
        self.assertTrue(adapter_batch.metadata["paper_only"])
        self.assertEqual(["BITGET", "WHITEBIT", "OKX_SPOT", "GATE", "MEXC", "KUCOIN", "BYBIT_SPOT"], adapter_batch.metadata["target_venues"])
        self.assertEqual(1, adapter_batch.metadata["secondary_cex_spot_strength"]["priceable_symbol_count"])
        self.assertEqual("BITGET", adapter_batch.observations[0]["venue"])


if __name__ == "__main__":
    unittest.main()
