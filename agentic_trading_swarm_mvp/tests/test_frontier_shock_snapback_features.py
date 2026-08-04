from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frontier_crypto_adapter as frontier  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402
from strategy_lab import generate_strategy_lab_candidates, ingest_strategy_lab_recommendation  # noqa: E402
from strategy_program import compile_observation_program, record_feature_snapshots  # noqa: E402


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings() -> dict:
    output = copy.deepcopy(DEFAULT_SETTINGS)
    output["allow_live_trading"] = False
    output["account_capabilities"]["crypto_spot"] = True
    return output


def snapback_program() -> dict:
    return {
        "type": "observation_program",
        "universe": {"market_types": ["spot"], "quotes": ["USD", "USDT", "USDC"]},
        "calculated_features": {
            "shock_sigma": "-return_60m_bps / max(volatility_60m_bps, 10)",
            "recovery_slope": "return_1m_bps - return_5m_bps / 5",
            "cost_adjusted_snapback": "max(0, min(-return_15m_bps, 150) - 2 * spread_bps)",
        },
        "entry_expression": (
            "microstructure_history_ready >= 1 and shock_sigma >= 1.5 "
            "and price_zscore_60m <= -1.25 and return_15m_bps <= -40 "
            "and return_60m_bps >= -500 and return_60m_bps <= -25 "
            "and return_1m_bps > 0 and return_5m_bps > 0 and recovery_slope > 0 "
            "and relative_volume_1m_60m >= 1.5 and spread_bps <= 8 "
            "and liquidity_score >= 0.65 and quality_score >= 60 and stale_minutes <= 2"
        ),
        "invalidation_expression": (
            "return_1m_bps <= -20 or spread_bps > 12 or return_60m_bps < -650 "
            "or price_zscore_60m > 0.5"
        ),
        "direction": "long",
        "edge_expression": "cost_adjusted_snapback",
        "score_expression": (
            "clip(40 + 10 * shock_sigma + recovery_slope "
            "+ 8 * min(relative_volume_1m_60m, 3) - spread_bps, 0, 100)"
        ),
        "route_surface": "spot",
    }


def observation(price: float, observed_at: str, **overrides) -> dict:
    row = {
        "inst_id": "OKX_SPOT:ABC-USDT",
        "instrument_id": "OKX_SPOT:ABC-USDT",
        "symbol": "ABC-USDT",
        "venue": "OKX_SPOT",
        "trade_type": "frontier_crypto_venue_map",
        "market_type": "spot",
        "asset_class": "crypto_spot",
        "base": "ABC",
        "quote": "USDT",
        "last": price,
        "spread_bps": 2.0,
        "liquidity_score": 0.8,
        "quality_score": 80.0,
        "quality_status": "verified",
        "stale_minutes": 0.0,
        "data_status": "reachable",
        "observed_at": observed_at,
        "price_source": "fixture",
    }
    row.update(overrides)
    return row


class FrontierShockSnapbackFeatureTests(unittest.TestCase):
    def test_closed_candles_produce_ready_intraday_features(self) -> None:
        rows = []
        for index in range(62):
            close = 100.0 if index < 61 else 100.5
            volume = 1000.0 if index < 61 else 2000.0
            rows.append([index * 60_000, "0", "0", "0", str(close), "0", "0", str(volume), "1"])
        features = frontier._intraday_features(
            {"parser": "okx_1m_candles"},
            {"ok": True, "payload": {"data": list(reversed(rows))}},
        )

        self.assertEqual("ready", features["microstructure_status"])
        self.assertEqual(1.0, features["microstructure_history_ready"])
        self.assertAlmostEqual(50.0, features["return_1m_bps"])
        self.assertEqual(2.0, features["relative_volume_1m_60m"])
        self.assertEqual(2000.0, features["quote_volume_1m"])

    def test_public_candle_shapes_exclude_the_forming_bar(self) -> None:
        binance_rows = []
        gate_rows = []
        for index in range(62):
            close = 100.5 if index == 60 else 100.0
            volume = 2000.0 if index == 60 else 1000.0
            binance_rows.append(
                [index * 60_000, "100", "101", "99", str(close), "10", (index + 1) * 60_000, str(volume)]
            )
            gate_rows.append([index * 60, str(volume), str(close), "101", "99", "100", "10"])

        for parser, payload in (
            ("binance_style_1m_klines", binance_rows),
            ("gate_1m_candles", gate_rows),
        ):
            with self.subTest(parser=parser):
                features = frontier._intraday_features(
                    {"parser": parser},
                    {"ok": True, "payload": payload},
                )
                self.assertEqual(1.0, features["microstructure_history_ready"])
                self.assertAlmostEqual(50.0, features["return_1m_bps"])
                self.assertEqual(2.0, features["relative_volume_1m_60m"])

    def test_intraday_enrichment_is_bounded_and_missing_history_stays_flat(self) -> None:
        cfg = settings()
        cfg["frontier_crypto_adapter"]["intraday_feature_max_observations"] = 1
        rows = [
            {
                "instrument_id": "OKX_SPOT:ABC-USDT",
                "venue": "OKX_SPOT",
                "market_type": "spot",
                "symbol": "ABC-USDT",
                "quote": "USDT",
                "last": 100.0,
                "quote_volume_24h": 10_000_000.0,
                "data_status": "reachable",
                "microstructure_history_ready": 0.0,
            },
            {
                "instrument_id": "OKX_SPOT:XYZ-USDT",
                "venue": "OKX_SPOT",
                "market_type": "spot",
                "symbol": "XYZ-USDT",
                "quote": "USDT",
                "last": 10.0,
                "quote_volume_24h": 5_000_000.0,
                "data_status": "reachable",
                "microstructure_history_ready": 0.0,
            },
        ]
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
        with mock.patch.object(
            frontier,
            "fetch_json",
            return_value={"ok": True, "payload": {"data": []}},
        ) as fetch:
            enriched, summary = frontier.enrich_intraday_features(rows, cfg, registry)

        self.assertEqual(1, fetch.call_count)
        self.assertEqual(1, summary["selected_count"])
        self.assertEqual(0, summary["ready_count"])
        self.assertEqual(0.0, enriched[0]["microstructure_history_ready"])
        self.assertEqual("insufficient_closed_candles", enriched[0]["microstructure_status"])
        self.assertEqual(0.0, enriched[1]["microstructure_history_ready"])

    def test_strategy_snapshot_persists_intraday_confirmation_features(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        ready = observation(
            100.0,
            now.isoformat(),
            return_1m_bps=12.5,
            quote_volume_1m=2500.0,
            relative_volume_1m_60m=1.75,
            microstructure_history_ready=1.0,
        )
        with memory_db() as conn:
            record_feature_snapshots(conn, [ready], settings())
            stored = json.loads(
                conn.execute("select features_json from strategy_feature_snapshots").fetchone()[0]
            )

        self.assertEqual(12.5, stored["return_1m_bps"])
        self.assertEqual(2500.0, stored["quote_volume_1m"])
        self.assertEqual(1.75, stored["relative_volume_1m_60m"])
        self.assertEqual(1.0, stored["microstructure_history_ready"])

    def test_snapback_program_compiles_and_emits_only_with_ready_confirmation(self) -> None:
        program, diagnostic = compile_observation_program(snapback_program())
        self.assertIsNotNone(program, diagnostic)
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        recommendation = {
            "recommendation_id": "rec_frontier_shock_snapback",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "strategy_lab_experiment": {
                    "strategy_lab_id": "frontier_liquid_spot_shock_snapback_v1",
                    "version": 1,
                    "experiment_type": "market_strategy",
                    "hypothesis": "Liquid spot downside shocks snap back after volume-backed recovery confirmation.",
                    "strategy_logic": snapback_program(),
                    "data_requirements": {"paper_only": True, "closed_1m_candles": 61},
                    "risk_gates": {"max_spread_bps": 8, "min_quality_score": 60},
                    "promotion_rules": {},
                },
            },
        }
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, recommendation, cfg)
            prices = [100.0] * 11 + [95.0]
            for index, price in enumerate(prices):
                minutes_ago = 60 - index * 5
                record_feature_snapshots(
                    conn,
                    [observation(price, (now - dt.timedelta(minutes=minutes_ago)).isoformat())],
                    cfg,
                )
            ready = observation(
                96.0,
                now.isoformat(),
                return_1m_bps=30.0,
                quote_volume_1m=2000.0,
                relative_volume_1m_60m=2.0,
                microstructure_history_ready=1.0,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                {ready["inst_id"]: ready},
            )

        self.assertEqual(1, len(generated), report)
        self.assertEqual("long_frontier_spot", generated[0]["direction"])
        self.assertEqual("observation_program", generated[0]["strategy_lab_logic_type"])
        self.assertEqual(2.0, generated[0]["strategy_lab_program_features"]["relative_volume_1m_60m"])


if __name__ == "__main__":
    unittest.main()
