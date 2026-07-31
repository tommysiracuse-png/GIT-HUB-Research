from __future__ import annotations

import copy
import collections
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import agent_review
import frontier_crypto_adapter as frontier
import frontier_data_quality as quality
import storage
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def observation() -> dict:
    return {
        "venue": "GATE",
        "market_type": "spot",
        "symbol": "ABC_USDT",
        "base": "ABC",
        "quote": "USDT",
        "comparison_key": "ABC",
        "instrument_id": "GATE:ABC_USDT",
        "route_id": "gate_spot_public",
        "data_status": "reachable",
        "http_status": "200",
        "latency_ms": 25.0,
        "last_checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bid": 99.99,
        "ask": 100.01,
        "last": 100.0,
        "quote_volume_24h": 2_000_000.0,
        "spread_bps": 2.0,
        "source_url": "https://example.test",
        "notes": [],
    }


def healthy_book() -> dict:
    return {
        "bids": [["99.99", "20"], ["99.98", "20"], ["99.95", "20"]],
        "asks": [["100.01", "20"], ["100.02", "20"], ["100.05", "20"]],
        "book_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "freshness_basis": "exchange_timestamp",
    }


class DepthParserTests(unittest.TestCase):
    def test_all_supported_depth_parsers(self) -> None:
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        cases = {
            "binance_depth": {"bids": [["99", "2"]], "asks": [["101", "2"]]},
            "mexc_depth": {"bids": [["99", "2"]], "asks": [["101", "2"]]},
            "coinbase_book": {"bids": [["99", "2", 1]], "asks": [["101", "2", 1]]},
            "kucoin_level2": {"data": {"bids": [["99", "2"]], "asks": [["101", "2"]], "time": now_ms}},
            "gate_order_book": {"bids": [["99", "2"]], "asks": [["101", "2"]], "current": now_ms},
            "bitget_orderbook": {"data": {"bids": [["99", "2"]], "asks": [["101", "2"]], "ts": str(now_ms)}},
            "kraken_depth": {"result": {"ABCUSDT": {"bids": [["99", "2", 1_780_000_000]], "asks": [["101", "2", 1_780_000_000]]}}},
            "okx_books": {"data": [{"bids": [["99", "2", "0", "1"]], "asks": [["101", "2", "0", "1"]], "ts": str(now_ms)}]},
            "luno_orderbook": {"bids": [{"price": "99", "volume": "2"}], "asks": [{"price": "101", "volume": "2"}]},
            "valr_orderbook": {"bids": [{"price": "99", "quantity": "2"}], "asks": [{"price": "101", "quantity": "2"}]},
            "quidax_depth": {"data": {"bids": [{"price": "99", "amount": "2"}], "asks": [{"price": "101", "amount": "2"}]}},
            "indodax_depth": {"buy": [["99", "2"]], "sell": [["101", "2"]]},
            "bitkub_depth": {"result": {"bids": [[1, 2, "2", "99"]], "asks": [[1, 2, "2", "101"]]}},
            "bybit_orderbook": {"result": {"b": [["99", "2"]], "a": [["101", "2"]], "ts": str(now_ms)}},
            "bitso_order_book": {"payload": {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "bids": [{"price": "99", "amount": "2"}], "asks": [{"price": "101", "amount": "2"}]}},
            "mercado_bitcoin_orderbook": {"bids": [["99", "2"]], "asks": [["101", "2"]]},
            "buda_order_book": {"order_book": {"bids": [["99", "2"]], "asks": [["101", "2"]]}},
        }

        for parser, payload in cases.items():
            with self.subTest(parser=parser):
                parsed = quality._extract_depth(parser, payload, dt.datetime.now(dt.timezone.utc).isoformat())
                self.assertTrue(parsed["bids"])
                self.assertTrue(parsed["asks"])
                self.assertIn(parsed["freshness_basis"], {"exchange_timestamp", "response_received"})

    def test_valr_uppercase_book_and_indodax_compact_symbol(self) -> None:
        parsed = quality._extract_depth(
            "valr_orderbook",
            {
                "Bids": [{"price": "99", "quantity": "2"}],
                "Asks": [{"price": "101", "quantity": "2"}],
                "LastChange": "2026-07-30T20:00:00Z",
            },
            "2026-07-30T20:00:01+00:00",
        )
        self.assertTrue(parsed["bids"])
        self.assertTrue(parsed["asks"])
        self.assertEqual("btcidr", quality._format_symbol("INDODAX", "BTC_IDR"))

    def test_http_200_business_errors_and_valid_empty_books_are_distinct(self) -> None:
        self.assertEqual(
            "api_error_payload:indodax_invalid_pair",
            quality._depth_payload_error("indodax_depth", {"error": "Invalid pair"}),
        )
        self.assertEqual(
            "api_error_payload:bitget_40015",
            quality._depth_payload_error("bitget_orderbook", {"code": "40015", "msg": "bad symbol"}),
        )
        self.assertEqual(
            "valid_empty_book",
            quality._empty_depth_reason("bitget_orderbook", {"code": "00000", "data": {"bids": [], "asks": []}}),
        )


class QualityMathTests(unittest.TestCase):
    def test_depth_aggregation_fill_and_quality_score(self) -> None:
        result = quality.analyze_book(
            observation(),
            healthy_book(),
            latency_ms=50.0,
            received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

        self.assertGreater(result["depth_usd"]["bid"]["10"], 1000)
        self.assertTrue(result["simulated_fills"]["buy"]["1000"]["filled"])
        self.assertTrue(result["simulated_fills"]["sell"]["1000"]["filled"])
        self.assertGreaterEqual(result["quality_score"], 60)
        self.assertEqual(result["critical_anomaly_flags"], [])

    def test_crossed_stale_malformed_book_is_degraded(self) -> None:
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)).isoformat()
        result = quality.analyze_book(
            observation(),
            {
                "bids": [["101", "2"], ["bad", "2"], ["101", "1"]],
                "asks": [["100", "2"]],
                "book_timestamp": stale,
                "freshness_basis": "exchange_timestamp",
            },
            latency_ms=2500.0,
            received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

        self.assertEqual(result["quality_status"], "degraded")
        self.assertIn("crossed_book", result["critical_anomaly_flags"])
        self.assertIn("stale_book", result["anomaly_flags"])
        self.assertIn("high_latency", result["anomaly_flags"])
        self.assertIn("invalid_level_value", result["anomaly_flags"])

    def test_regional_depth_is_converted_to_usd_before_quality_scoring(self) -> None:
        row = observation()
        row.update(
            {
                "quote": "ZAR",
                "last": 1900.0,
                "bid": 1899.0,
                "ask": 1901.0,
                "usd_normalized_last": 100.0,
                "quote_normalization_status": "external_fx_reference",
            }
        )
        result = quality.analyze_book(
            row,
            {
                "bids": [[1899.0, 2.0]],
                "asks": [[1901.0, 2.0]],
                "book_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "freshness_basis": "exchange_timestamp",
            },
            latency_ms=20.0,
            received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.assertAlmostEqual(100.0 / 1900.0, result["quote_to_usd_multiplier"], places=9)
        self.assertLess(result["depth_usd"]["bid"]["25"], 250.0)
        self.assertGreater(result["depth_usd"]["bid"]["25"], 190.0)

    def test_regional_depth_without_fx_is_unknown_not_fake_usd(self) -> None:
        row = observation()
        row.update({"quote": "IDR", "last": 1_000_000.0, "bid": 999_000.0, "ask": 1_001_000.0})
        result = quality.analyze_book(
            row,
            {
                "bids": [[999_000.0, 1.0]],
                "asks": [[1_001_000.0, 1.0]],
                "book_timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "freshness_basis": "exchange_timestamp",
            },
            latency_ms=20.0,
            received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.assertEqual("unknown", result["quality_status"])
        self.assertIn("missing_fx_conversion", result["anomaly_flags"])
        self.assertEqual(0.0, result["depth_usd"]["bid"]["25"])

    def test_candidate_uses_conservative_depth_adjusted_cost(self) -> None:
        cfg = settings()
        row = observation()
        row.update(
            quality.analyze_book(
                row,
                healthy_book(),
                latency_ms=25.0,
                received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        )

        candidate = frontier._candidate_from_observation(row, cfg, 99.0, 4)

        self.assertEqual(candidate["estimated_fee_bps_per_side"], 10.0)
        self.assertEqual(candidate["frontier_cost_source"], "public_order_book")
        self.assertGreaterEqual(candidate["estimated_round_trip_cost_bps"], 20.0)
        self.assertEqual(candidate["quality_action"], "normal")
        self.assertTrue(candidate["promotion_eligible"])

    def test_unknown_depth_is_shadow_only_and_promotion_ineligible(self) -> None:
        candidate = frontier._candidate_from_observation(observation(), settings(), 99.0, 4)

        self.assertEqual(candidate["quality_status"], "unknown")
        self.assertEqual(candidate["quality_action"], "shadow_only")
        self.assertTrue(candidate["paper_entry_blocked"])
        self.assertFalse(candidate["promotion_eligible"])

    def test_quality_probation_caps_paper_allocation(self) -> None:
        cfg = settings()
        cfg["account_capabilities"]["spot_borrow"] = True
        row = observation()
        row["quote_volume_24h"] = 10_000_000.0
        row.update(
            {
                "quality_status": "degraded",
                "quality_score": 50.0,
                "freshness_age_seconds": 5.0,
                "simulated_fills": {
                    "buy": {"1000": {"filled": True, "slippage_bps": 2.0}},
                    "sell": {"1000": {"filled": True, "slippage_bps": 2.0}},
                },
                "anomaly_flags": ["unsorted_levels"],
                "critical_anomaly_flags": [],
            }
        )
        candidate = frontier._candidate_from_observation(row, cfg, 99.0, 4)
        review = agent_review.review_candidate(candidate, cfg, {})

        self.assertEqual(candidate["quality_action"], "conditional")
        self.assertEqual(review["decision"], "approve_conditional_paper_trade")
        self.assertEqual(review["paper_allocation_multiplier"], 0.25)


class QualityPersistenceTests(unittest.TestCase):
    def test_snapshot_retention_and_venue_scoring(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"]["snapshot_retention_rows"] = 2
        rows = []
        for index in range(3):
            row = observation()
            row["instrument_id"] = f"GATE:ABC{index}_USDT"
            row.update(
                quality.analyze_book(
                    row,
                    healthy_book(),
                    latency_ms=20.0 + index,
                    received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                )
            )
            rows.append(row)

        result = quality.persist_quality_snapshots(conn, rows, cfg)
        leaderboard = quality.venue_quality_scores(conn)

        self.assertEqual(conn.execute("select count(*) from frontier_quality_snapshots").fetchone()[0], 2)
        self.assertEqual(result["snapshot_rows_deleted"], 1)
        self.assertEqual(leaderboard[0]["venue"], "GATE")
        self.assertGreater(leaderboard[0]["venue_quality_score"], 0)

    def test_market_testing_progress_counts_recent_depth_snapshots(self) -> None:
        conn = memory_conn()
        cfg = settings()
        row = observation()
        row["instrument_id"] = "GATE:PROGRESS_USDT"
        row.update(
            quality.analyze_book(
                row,
                healthy_book(),
                latency_ms=20.0,
                received_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        )
        quality.persist_quality_snapshots(conn, [row], cfg)

        progress = quality.market_testing_progress(conn)

        self.assertEqual(progress["last_hour"]["markets_tested"], 1)
        self.assertEqual(progress["last_hour"]["venues_tested"], 1)
        self.assertEqual(progress["last_hour"]["new_markets_tested"], 1)
        self.assertEqual(progress["last_hour"]["new_venues_tested"], 1)
        self.assertEqual(progress["last_24h"]["known_quality_markets"], 1)

    def test_quality_relationship_uses_only_valid_60m_outcomes(self) -> None:
        conn = memory_conn()
        candidate = {
            "quality_score": 75.0,
            "venue": "GATE",
            "inst_id": "GATE:ABC_USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "score": 60.0,
            "last": 100.0,
            "execution_feasibility": {"status": "conditional"},
        }
        review = {"learned_score": 60.0}
        trade_id = storage.open_paper_trade(conn, candidate, review)
        conn.execute(
            """
            insert into paper_trade_outcomes (
                trade_id, horizon_minutes, measured_at, price, pnl_bps,
                context_json, target_at, observed_at, delay_seconds,
                measurement_status, price_source
            ) values (?, 60, 'now', 99, 25, '{}', 'now', 'now', 10, 'valid', 'test')
            """,
            (trade_id,),
        )
        conn.commit()

        relationship = quality.quality_outcome_relationship(conn)

        self.assertEqual(relationship[0]["quality_bucket"], "60-79")
        self.assertEqual(relationship[0]["avg_pnl_bps"], 25.0)


class EnrichmentSelectionTests(unittest.TestCase):
    def test_adaptive_selection_prefers_less_sampled_regional_instruments(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"]["max_symbols_per_cycle"] = 1
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for index in range(3):
            conn.execute(
                """
                insert into frontier_quality_snapshots (
                    bucket_at, observed_at, venue, inst_id, quality_status,
                    quality_score, latency_ms, freshness_age_seconds, spread_bps,
                    bid_depth_10bps_usd, ask_depth_10bps_usd,
                    buy_slippage_1000_bps, sell_slippage_1000_bps,
                    anomaly_json, metrics_json
                ) values (?, ?, 'LUNO', 'LUNO:HIGHZAR', 'verified', 90, 10, 1, 2, 1000, 1000, 0, 0, '[]', '{}')
                """,
                (f"2026-06-30T00:{index:02d}:00+00:00", now),
            )
        conn.commit()
        high = observation()
        high.update({"venue": "LUNO", "instrument_id": "LUNO:HIGHZAR", "symbol": "HIGHZAR", "quote": "ZAR", "region": "Africa", "quote_volume_24h": 10_000_000})
        low = observation()
        low.update({"venue": "LUNO", "instrument_id": "LUNO:LOWZAR", "symbol": "LOWZAR", "quote": "ZAR", "region": "Africa", "quote_volume_24h": 1_000_000})

        selected = quality.select_enrichment_observations(conn, [high, low], [], {}, cfg)

        self.assertEqual([row["instrument_id"] for row in selected], ["LUNO:LOWZAR"])

    def test_expansion_selector_uses_reserved_buckets(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"].update(
            {
                "max_symbols_per_cycle": 5,
                "max_symbols_per_venue": 2,
                "quality_target_escalation_enabled": False,
                "exploit_variant_reserve_per_cycle": 1,
                "regional_reserve_per_cycle": 1,
                "unknown_quality_reserve_per_cycle": 1,
                "starved_venues": ["LUNO", "KRAKEN", "COINBASE"],
                "starved_venue_min_depth_per_cycle": 0,
                "venue_depth_minimums": {},
            }
        )
        open_obs = observation()
        open_obs.update({"venue": "GATE", "instrument_id": "GATE:OPEN", "symbol": "OPEN_USDT"})
        active_obs = observation()
        active_obs.update({"venue": "MEXC", "instrument_id": "MEXC:ACTIVE", "symbol": "ACTIVE_USDT"})
        regional_obs = observation()
        regional_obs.update({"venue": "LUNO", "instrument_id": "LUNO:REGZAR", "symbol": "REGZAR", "quote": "ZAR", "region": "Africa"})
        unknown_obs = observation()
        unknown_obs.update({"venue": "KRAKEN", "instrument_id": "KRAKEN:UNKUSD", "symbol": "UNKUSD", "quote": "USD"})
        dislocation_obs = observation()
        dislocation_obs.update({"venue": "COINBASE", "instrument_id": "COINBASE:DISLOC-USD", "symbol": "DISLOC-USD", "quote": "USD"})
        storage.open_paper_trade(
            conn,
            {
                "inst_id": "GATE:OPEN",
                "trade_type": "frontier_crypto_venue_map",
                "direction": "long_frontier_spot",
                "venue": "GATE",
                "last": 100.0,
                "score": 50.0,
                "execution_feasibility": {"status": "standard"},
            },
            {"learned_score": 50.0},
        )

        selected = quality.select_enrichment_observations(
            conn,
            [open_obs, active_obs, regional_obs, unknown_obs, dislocation_obs],
            [{"variant_id": "active", "status": "active"}],
            {
                "active": [{"inst_id": "MEXC:ACTIVE", "venue_deviation_bps": 20.0}],
                "raw": [{"inst_id": "COINBASE:DISLOC-USD", "venue_deviation_bps": 50.0}],
            },
            cfg,
        )

        buckets = {row["instrument_id"]: row.get("depth_selection_bucket") for row in selected}
        self.assertEqual(len(selected), 5)
        self.assertEqual(buckets["GATE:OPEN"], "open_paper_trade")
        self.assertEqual(buckets["MEXC:ACTIVE"], "exploit_more_or_variant")
        self.assertEqual(buckets["LUNO:REGZAR"], "regional_frontier")
        self.assertEqual(buckets["KRAKEN:UNKUSD"], "unknown_quality_high_volume")
        self.assertEqual(buckets["COINBASE:DISLOC-USD"], "largest_dislocation")

    def test_expansion_selector_respects_per_venue_cap(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"].update(
            {
                "max_symbols_per_cycle": 10,
                "max_symbols_per_venue": 2,
                "quality_target_escalation_enabled": False,
                "unknown_quality_reserve_per_cycle": 10,
            }
        )
        observations = []
        for index in range(5):
            row = observation()
            row.update({"venue": "MEXC", "instrument_id": f"MEXC:SYM{index}_USDT", "symbol": f"SYM{index}_USDT"})
            observations.append(row)

        selected = quality.select_enrichment_observations(conn, observations, [], {}, cfg)

        self.assertEqual(len(selected), 2)

    def test_quality_target_escalator_expands_bounded_selection_when_coverage_is_low(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"].update(
            {
                "max_symbols_per_cycle": 3,
                "max_symbols_per_venue": 3,
                "quality_target_escalation_enabled": True,
                "known_quality_rate_target": 0.25,
                "quality_target_extra_symbols_per_cycle": 2,
                "quality_target_max_symbols_per_cycle": 5,
                "quality_target_max_symbols_per_venue": 5,
                "starved_venues": ["MEXC"],
            }
        )
        observations = []
        for index in range(5):
            row = observation()
            row.update({"venue": "MEXC", "instrument_id": f"MEXC:LOWQ{index}_USDT", "symbol": f"LOWQ{index}_USDT"})
            observations.append(row)

        selected = quality.select_enrichment_observations(conn, observations, [], {}, cfg)

        self.assertEqual(len(selected), 5)
        escalation = selected[0]["depth_selection_escalation"]
        self.assertTrue(escalation["active"])
        self.assertEqual(escalation["max_symbols_per_cycle"], 5)
        self.assertEqual(escalation["extra_symbols_requested"], 2)

    def test_starved_venue_minimums_reserve_per_venue_depth_samples(self) -> None:
        conn = memory_conn()
        cfg = settings()
        cfg["frontier_data_quality"].update(
            {
                "max_symbols_per_cycle": 12,
                "max_symbols_per_venue": 5,
                "quality_target_escalation_enabled": False,
                "unknown_quality_reserve_per_cycle": 0,
                "regional_reserve_per_cycle": 0,
                "exploit_variant_reserve_per_cycle": 0,
                "starved_venues": ["BITSO", "KRAKEN"],
                "starved_venue_min_depth_per_cycle": 0,
                "venue_depth_minimums": {"BITSO": 3, "KRAKEN": 2},
            }
        )
        observations = []
        for venue, count in (("BITSO", 5), ("KRAKEN", 4), ("GATE", 6)):
            for index in range(count):
                row = observation()
                row.update(
                    {
                        "venue": venue,
                        "instrument_id": f"{venue}:Q{index}_USDT",
                        "symbol": f"Q{index}_USDT",
                        "quote_volume_24h": 10_000 - index,
                    }
                )
                observations.append(row)

        selected = quality.select_enrichment_observations(conn, observations, [], {}, cfg)

        counts = collections.Counter(row["venue"] for row in selected)
        buckets = collections.Counter(row["depth_selection_bucket"] for row in selected)
        quota_report = selected[0]["depth_selection_venue_quota_report"]
        self.assertGreaterEqual(counts["BITSO"], 3)
        self.assertGreaterEqual(counts["KRAKEN"], 2)
        self.assertEqual(buckets["starved_venue_minimum"], 5)
        self.assertEqual(quota_report["BITSO"]["status"], "met")
        self.assertEqual(quota_report["KRAKEN"]["status"], "met")
        self.assertEqual(selected[0]["depth_selection_limits"]["max_symbols_per_cycle"], 12)


if __name__ == "__main__":
    unittest.main()
