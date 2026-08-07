from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import storage  # noqa: E402
import radar_loop  # noqa: E402
from paired_direct_contract import (  # noqa: E402
    validate_paired_direct_outcome_provenance,
    validate_paired_funding_coverage,
)


UTC = dt.timezone.utc


def bounded_settings(horizons: list[int] | None = None, max_delay: int = 300) -> dict:
    return {
        "mode": "paper",
        "allow_live_trading": False,
        "operations": {"fail_closed_recovery_profile": True},
        "paper_expansion": {"enabled": True},
        "market_admission": {"enabled": True, "paper_queue_enabled": True},
        "learning": {
            "horizon_minutes": list(horizons or [15]),
            "max_outcome_delay_seconds": max_delay,
        },
        "risk": {
            "taker_fee_bps_per_leg": 0.0,
            "slippage_bps_per_leg": 0.0,
        },
    }


def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def direct_candidate(inst_id: str = "BTC_USDT") -> dict:
    return {
        "venue": "GATE",
        "inst_id": inst_id,
        "market_surface": "spot",
        "trade_type": "frontier_crypto_venue_map",
        "direction": "long_frontier_spot",
        "last": 100.0,
        "score": 80.0,
        "route_status": "standard",
        "signal_stats_scope": "direct",
        "execution_feasibility": {"status": "standard", "route_status": "standard"},
    }


def paired_candidate(entry_at: dt.datetime) -> dict:
    source = {
        "name": "OKX public REST",
        "endpoint": "/api/v5/market/ticker",
        "parser": "okx_direct_quote",
        "event_id": "entry-event",
    }
    return {
        "venue": "OKX",
        "inst_id": "BTC-USDT-SWAP",
        "market_surface": "perp_funding_basis",
        "trade_type": "perp_funding_basis",
        "direction": "short_perp_long_spot",
        "last": 100.0,
        "score": 80.0,
        "route_status": "standard",
        "signal_stats_scope": "direct",
        "execution_feasibility": {"status": "standard", "route_status": "standard"},
        "paired_direct_v1": {
            "contract_version": "paired_direct_v1",
            "strategy_family": "okx_short_perp_long_spot_basis",
            "status": "entry_complete",
            "accounting_convention": "direct_reference_returns_minus_declared_costs_v1",
            "quote_asset": "USDT",
            "max_entry_timestamp_skew_seconds": 2.0,
            "notional_match_tolerance_fraction": 0.01,
            "declared_gross_notional_usd": 100.0,
            "return_denominator_usd": 100.0,
            "entry_components": {
                "perp": {
                    "side": "short",
                    "venue": "OKX",
                    "market_surface": "perp",
                    "inst_id": "BTC-USDT-SWAP",
                    "quote_asset": "USDT",
                    "event_at": entry_at.isoformat(),
                    "price": 100.0,
                    "notional_usd": 50.0,
                    "entry_fee_bps": 0.0,
                    "entry_slippage_bps": 0.0,
                    "exit_fee_bps": 0.0,
                    "exit_slippage_bps": 0.0,
                    "source": dict(source),
                },
                "spot": {
                    "side": "long",
                    "venue": "OKX_SPOT",
                    "market_surface": "spot",
                    "inst_id": "BTC-USDT",
                    "quote_asset": "USDT",
                    "event_at": entry_at.isoformat(),
                    "price": 100.0,
                    "notional_usd": 50.0,
                    "entry_fee_bps": 0.0,
                    "entry_slippage_bps": 0.0,
                    "exit_fee_bps": 0.0,
                    "exit_slippage_bps": 0.0,
                    "source": {**source, "event_id": "spot-entry-event"},
                },
            },
            "funding_requirement": {
                "required": True,
                "venue": "OKX",
                "inst_id": "BTC-USDT-SWAP",
                "source_endpoint": "/api/v5/public/funding-rate-history",
                "source_parser": "okx_realized_funding_history",
                "allow_estimates": False,
            },
        },
    }


def insert_bounded_trade(
    conn: sqlite3.Connection,
    opened_at: dt.datetime,
    *,
    candidate: dict | None = None,
    selected_hold_minutes: int = 15,
) -> int:
    candidate = copy.deepcopy(candidate or direct_candidate())
    ordinal = int(conn.execute("select count(*) from paper_trades").fetchone()[0]) + 1
    admission_key = f"admission-{ordinal}"
    episode_id = f"episode-{ordinal}"
    execution_order_id = 10_000 + ordinal
    context = {
        "signal_stats_scope": "direct",
        "route_status": "standard",
        "feasibility_status": "standard",
        "selected_hold_minutes": selected_hold_minutes,
    }
    cursor = conn.execute(
        """
        insert into paper_trades (
            opened_at,venue,inst_id,direction,trade_type,signal_key,base_score,
            learned_score,entry,status,thesis,candidate_json,review_json,
            execution_order_id,route_id,entry_fee_bps,entry_slippage_bps,
            context_json,selected_hold_minutes,hold_decision_json,
            admission_key,admission_episode_id
            ) values (?,?,?,?,?,?,?,?,?,'open','test',?, '{}',?,'direct',0,0,?,?,?,?,?)
        """,
        (
            opened_at.isoformat(),
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate["trade_type"],
            "test|direct|standard",
            float(candidate["score"]),
            float(candidate["score"]),
            float(candidate["last"]),
            json.dumps(candidate, sort_keys=True),
            execution_order_id,
            json.dumps(context, sort_keys=True),
            selected_hold_minutes,
            json.dumps({"hold_minutes": selected_hold_minutes, "source": "test"}),
            admission_key,
            episode_id,
        ),
    )
    trade_id = int(cursor.lastrowid)
    now_iso = opened_at.isoformat()
    conn.execute(
        """
        insert into paper_admission_queue (
            queue_id,admission_key,episode_id,evidence_fingerprint,evidence_observed_at,
            lane,status,priority,venue,inst_id,market_surface,lineage_root,direction,
            route_status,candidate_json,eligibility_json,enqueued_at,updated_at,
            execution_order_id,paper_trade_id
        ) values (?,?,?,?,?,'direct','waiting_outcome',1,?,?,?,?,?,'standard',?,'{}',?,?,?,?)
        """,
        (
            f"queue-{ordinal}",
            admission_key,
            episode_id,
            f"evidence-{ordinal}",
            now_iso,
            candidate["venue"],
            candidate["inst_id"],
            candidate["market_surface"],
            f"lineage-{ordinal}",
            candidate["direction"],
            json.dumps(candidate, sort_keys=True),
            now_iso,
            now_iso,
            execution_order_id,
            trade_id,
        ),
    )
    conn.commit()
    return trade_id


def candle(
    candle_open_at: dt.datetime,
    price: float,
    *,
    venue: str = "GATE",
    inst_id: str = "BTC_USDT",
    market_surface: str = "spot",
    received_at: dt.datetime | None = None,
    source_event_id: str | None = None,
) -> dict:
    parser_by_venue = {
        "GATE": ("gate_1m_candles", "/api/v4/spot/candlesticks", "Gate public REST spot candlesticks"),
        "OKX": ("okx_1m_candles", "/api/v5/market/history-candles", "OKX public REST history candles"),
        "OKX_SPOT": ("okx_1m_candles", "/api/v5/market/history-candles", "OKX public REST history candles"),
    }
    parser, endpoint, source_name = parser_by_venue[venue]
    event_at = candle_open_at + dt.timedelta(minutes=1)
    return {
        "source_kind": "exchange_candle_1m_close",
        "venue": venue,
        "inst_id": inst_id,
        "market_surface": market_surface,
        "candle_open_at": candle_open_at.isoformat(),
        "event_at": event_at.isoformat(),
        "received_at": (received_at or event_at).isoformat(),
        "price": price,
        "source_name": source_name,
        "source_parser": parser,
        "source_endpoint": endpoint,
        "source_event_id": source_event_id or f"{venue}|{inst_id}|{candle_open_at.isoformat()}",
        "is_closed": True,
        "is_partial": False,
        "freshness_state": "fresh",
        "quality_status": "verified",
    }


def funding_coverage(entry_at: dt.datetime, exit_at: dt.datetime) -> dict:
    request_url = (
        "https://www.okx.com/api/v5/public/funding-rate-history?"
        "instId=BTC-USDT-SWAP&limit=400"
    )
    received_at = exit_at + dt.timedelta(minutes=1)
    payload_sha256 = hashlib.sha256(b"qualified-funding-response").hexdigest()
    query_identity = {
        "request_url": request_url,
        "requested_from": entry_at.isoformat(),
        "requested_through": exit_at.isoformat(),
        "received_at": received_at.isoformat(),
        "payload_sha256": payload_sha256,
    }
    query_id = "okx-funding-query-" + hashlib.sha256(
        json.dumps(query_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "venue": "OKX",
        "inst_id": "BTC-USDT-SWAP",
        "coverage_status": "complete",
        "allow_estimates": False,
        "complete_from": entry_at.isoformat(),
        "complete_through": exit_at.isoformat(),
        "source": {
            "name": "OKX public REST realized funding history",
            "endpoint": "/api/v5/public/funding-rate-history",
            "parser": "okx_realized_funding_history",
            "inst_id": "BTC-USDT-SWAP",
        },
        "query": {
            "query_id": query_id,
            "request_url": request_url,
            "requested_from": entry_at.isoformat(),
            "requested_through": exit_at.isoformat(),
            "received_at": received_at.isoformat(),
            "request_succeeded": True,
            "http_status": 200,
            "page_count": 1,
            "pagination_complete": True,
            "range_complete": True,
            "payload_sha256": payload_sha256,
        },
        "events": [],
    }


class PaperOutcomeStorageTests(unittest.TestCase):
    def test_radar_collection_path_journals_both_pair_legs_funding_and_then_closes(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        trade_id = insert_bounded_trade(conn, base, candidate=paired_candidate(base))
        cfg = bounded_settings(max_delay=300)
        exit_open = base + dt.timedelta(minutes=15)
        exit_at = exit_open + dt.timedelta(minutes=1)

        def fake_collector(provider, *, settings):
            del settings
            targets = provider.load_due_instruments(limit=100)
            self.assertEqual({"perp", "spot"}, {row["paired_component"] for row in targets})
            self.assertEqual(1, sum(bool(row["requires_funding_events"]) for row in targets))
            return {
                "enabled": True,
                "records": [
                    candle(
                        exit_open,
                        99.0,
                        venue="OKX",
                        inst_id="BTC-USDT-SWAP",
                        market_surface="perpetual_swap",
                    ),
                    candle(
                        exit_open,
                        101.0,
                        venue="OKX_SPOT",
                        inst_id="BTC-USDT",
                        market_surface="spot",
                    ),
                ],
                "funding_events": [],
                "funding_coverage": [funding_coverage(base, exit_at)],
                "attempted_window_keys": sorted(
                    {str(row["due_window_key"]) for row in targets}
                ),
                "rejections": [],
                "deferred_outcome_keys": [],
                "loaded_due_count": len(targets),
                "unique_instrument_count": 2,
                "fetched_instrument_count": 2,
                "funding_fetch_count": 1,
                "total_public_request_count": 3,
                "limits": {"max_instruments": 100, "max_workers": 4},
            }

        collection = radar_loop._collect_and_persist_due_outcomes(
            conn,
            cfg,
            collector=fake_collector,
        )
        self.assertEqual("persisted", collection["status"])
        self.assertEqual(2, collection["price_persistence"]["accepted"])
        self.assertEqual(1, collection["funding_persistence"]["accepted"])
        self.assertEqual(2, conn.execute("select count(*) from paper_price_observations").fetchone()[0])
        self.assertEqual(1, conn.execute("select count(*) from paper_funding_coverage_batches").fetchone()[0])
        self.assertEqual(2, conn.execute("select sum(attempt_count) from paper_outcome_due_cursors").fetchone()[0])

        recorded = storage.record_due_horizon_outcomes(
            conn, {}, cfg, now=base + dt.timedelta(minutes=30)
        )
        closed = storage.close_due_trades(conn, {}, 15, cfg)
        self.assertEqual(1, len(recorded))
        self.assertEqual(1, len(closed))
        trade = conn.execute(
            "select status,pnl_bps,context_json from paper_trades where id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("closed", trade["status"])
        self.assertAlmostEqual(100.0, trade["pnl_bps"], places=3)
        close_context = json.loads(trade["context_json"])
        self.assertTrue(close_context["paired_direct_close_validation"]["valid"])
        self.assertEqual(2, len(close_context["paired_direct_close_validation"]["observation_ids"]))

    def test_journal_rejects_unqualified_stale_partial_and_conflicting_sources(self) -> None:
        conn = memory_conn()
        opened = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        valid = candle(opened, 100.0, source_event_id="stable-event")
        fake_parser = {**valid, "source_event_id": "fake-parser", "source_parser": "trusted_by_caller"}
        fake_endpoint = {**valid, "source_event_id": "fake-endpoint", "source_endpoint": "/ticker"}
        stale = {**valid, "source_event_id": "stale", "freshness_state": "stale"}
        partial = {**valid, "source_event_id": "partial", "is_partial": True}

        result = storage.record_paper_price_observations(
            conn, [valid, fake_parser, fake_endpoint, stale, partial]
        )

        self.assertEqual(1, result["accepted"])
        self.assertEqual(4, result["rejected"])
        reasons = {item["reason"] for item in result["rejections"]}
        self.assertIn("unqualified_candle_source_tuple", reasons)
        self.assertIn("fresh_candle_required", reasons)
        self.assertIn("partial_candle_rejected", reasons)
        duplicate = storage.record_paper_price_observations(conn, [valid])
        self.assertEqual(1, duplicate["duplicates"])
        conflict = storage.record_paper_price_observations(conn, [{**valid, "price": 101.0}])
        self.assertEqual("source_event_payload_conflict", conflict["rejections"][0]["reason"])
        self.assertEqual(1, conn.execute("select count(*) from paper_price_observations").fetchone()[0])

    def test_selector_rejects_pre_target_and_uses_event_time_despite_late_receipt(self) -> None:
        conn = memory_conn()
        target = dt.datetime(2026, 8, 7, 12, 15, tzinfo=UTC)
        pre_target = candle(target - dt.timedelta(minutes=2), 90.0)
        on_time = candle(
            target,
            101.0,
            received_at=target + dt.timedelta(hours=2),
            source_event_id="received-late",
        )
        storage.record_paper_price_observations(conn, [pre_target, on_time])

        selected = storage.select_paper_price_observation(
            conn, "GATE", "BTC_USDT", target, 120, "spot"
        )

        self.assertIsNotNone(selected)
        self.assertEqual(101.0, selected["price"])
        self.assertEqual((target + dt.timedelta(minutes=1)).isoformat(), selected["event_at"])
        self.assertEqual((target + dt.timedelta(hours=2)).isoformat(), selected["received_at"])

    def test_two_same_instrument_targets_select_distinct_earliest_candles(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        first_id = insert_bounded_trade(conn, base)
        second_id = insert_bounded_trade(conn, base + dt.timedelta(minutes=2))
        storage.record_paper_price_observations(
            conn,
            [
                candle(base + dt.timedelta(minutes=15), 101.0, source_event_id="first-close"),
                candle(base + dt.timedelta(minutes=17), 102.0, source_event_id="second-close"),
            ],
        )

        recorded = storage.record_due_horizon_outcomes(
            conn,
            {"BTC_USDT": {"last": 999.0, "observed_at": (base + dt.timedelta(hours=1)).isoformat()}},
            bounded_settings(max_delay=120),
            now=base + dt.timedelta(hours=1),
        )

        self.assertEqual(2, len(recorded))
        rows = conn.execute(
            "select trade_id,price,observed_at,price_observation_id from paper_trade_outcomes order by trade_id"
        ).fetchall()
        self.assertEqual([first_id, second_id], [row["trade_id"] for row in rows])
        self.assertEqual([101.0, 102.0], [row["price"] for row in rows])
        self.assertTrue(all(row["price_observation_id"] for row in rows))

    def test_missing_outcome_upgrades_and_expired_trade_closes_once(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        trade_id = insert_bounded_trade(conn, base)
        cfg = bounded_settings(max_delay=300)

        first = storage.record_due_horizon_outcomes(
            conn,
            {
                "BTC_USDT": {
                    "last": 999.0,
                    "observed_at": (base + dt.timedelta(minutes=16)).isoformat(),
                    "price_source": "latest_ticker_must_not_label",
                }
            },
            cfg,
            now=base + dt.timedelta(minutes=21),
        )
        outcome_before = conn.execute(
            "select id,measurement_status,price from paper_trade_outcomes where trade_id=?", (trade_id,)
        ).fetchone()
        self.assertEqual("missing", outcome_before["measurement_status"])
        self.assertIsNone(outcome_before["price"])
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(storage.close_due_trades(conn, {}, 15, cfg)))
        self.assertEqual(
            "expired_unpriced",
            conn.execute("select status from paper_trades where id=?", (trade_id,)).fetchone()[0],
        )

        storage.record_paper_price_observations(
            conn,
            [
                candle(
                    base + dt.timedelta(minutes=15),
                    101.0,
                    received_at=base + dt.timedelta(hours=2),
                )
            ],
        )
        upgraded = storage.record_due_horizon_outcomes(
            conn, {}, cfg, now=base + dt.timedelta(hours=2)
        )
        outcome_after = conn.execute(
            "select id,measurement_status,price_observation_id from paper_trade_outcomes where trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(outcome_before["id"], outcome_after["id"])
        self.assertEqual("valid", outcome_after["measurement_status"])
        self.assertTrue(outcome_after["price_observation_id"])
        self.assertTrue(upgraded[0]["backfilled"])
        self.assertEqual(1, len(storage.close_due_trades(conn, {}, 15, cfg)))
        self.assertEqual("closed", conn.execute("select status from paper_trades where id=?", (trade_id,)).fetchone()[0])
        self.assertEqual([], storage.close_due_trades(conn, {}, 15, cfg))
        self.assertEqual([], storage.record_due_horizon_outcomes(conn, {}, cfg, now=base + dt.timedelta(hours=3)))

    def test_paired_trade_stays_missing_until_two_candles_and_durable_funding_exist(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        trade_id = insert_bounded_trade(conn, base, candidate=paired_candidate(base))
        cfg = bounded_settings(max_delay=300)
        exit_open = base + dt.timedelta(minutes=15)
        exit_at = exit_open + dt.timedelta(minutes=1)
        due_targets = storage.load_due_paper_outcome_targets(
            conn, cfg, now=base + dt.timedelta(minutes=20), limit=1
        )
        self.assertEqual(2, len(due_targets))
        self.assertEqual(1, len({target["parent_outcome_key"] for target in due_targets}))
        self.assertEqual(2, len({target["outcome_key"] for target in due_targets}))
        by_component = {target["paired_component"]: target for target in due_targets}
        self.assertEqual(
            ("OKX", "BTC-USDT-SWAP", "perpetual_swap", True, base.isoformat()),
            (
                by_component["perp"]["venue"],
                by_component["perp"]["inst_id"],
                by_component["perp"]["market_surface"],
                by_component["perp"]["requires_funding_events"],
                by_component["perp"]["funding_window_start_at"],
            ),
        )
        self.assertEqual(
            ("OKX_SPOT", "BTC-USDT", "spot", False, None),
            (
                by_component["spot"]["venue"],
                by_component["spot"]["inst_id"],
                by_component["spot"]["market_surface"],
                by_component["spot"]["requires_funding_events"],
                by_component["spot"]["funding_window_start_at"],
            ),
        )
        storage.record_paper_price_observations(
            conn,
            [
                candle(
                    exit_open,
                    99.0,
                    venue="OKX",
                    inst_id="BTC-USDT-SWAP",
                    market_surface="perpetual_swap",
                )
            ],
        )

        storage.record_due_horizon_outcomes(conn, {}, cfg, now=base + dt.timedelta(minutes=21))
        missing = conn.execute(
            "select measurement_status,context_json from paper_trade_outcomes where trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("missing", missing["measurement_status"])
        self.assertIn("spot_candle_unavailable", json.loads(missing["context_json"])["paper_outcome_missing_reason"])

        storage.record_paper_price_observations(
            conn,
            [
                candle(
                    exit_open,
                    101.0,
                    venue="OKX_SPOT",
                    inst_id="BTC-USDT",
                    market_surface="spot",
                )
            ],
        )
        qualified_coverage = funding_coverage(base, exit_at)
        evil_host = copy.deepcopy(qualified_coverage)
        evil_host["query"]["request_url"] = evil_host["query"]["request_url"].replace(
            "www.okx.com", "evil.example"
        )
        self.assertEqual(
            "funding_query_request_url_required",
            storage.record_paper_funding_coverage(conn, evil_host)["rejections"][0]["reason"],
        )
        wrong_inst = copy.deepcopy(qualified_coverage)
        wrong_inst["query"]["request_url"] = wrong_inst["query"]["request_url"].replace(
            "BTC-USDT-SWAP", "ETH-USDT-SWAP"
        )
        self.assertEqual(
            "funding_query_identity_mismatch",
            storage.record_paper_funding_coverage(conn, wrong_inst)["rejections"][0]["reason"],
        )
        funding_result = storage.record_paper_funding_coverage(conn, qualified_coverage)
        self.assertEqual(1, funding_result["accepted"])
        selected_coverage = storage.select_paper_funding_coverage(
            conn, "OKX", "BTC-USDT-SWAP", base, exit_at
        )
        self.assertIsNotNone(selected_coverage)
        self.assertIn("query", selected_coverage)
        self.assertTrue(
            validate_paired_funding_coverage(
                paired_candidate(base)["paired_direct_v1"], selected_coverage, exit_at
            )["valid"]
        )
        upgraded = storage.record_due_horizon_outcomes(
            conn, {}, cfg, now=base + dt.timedelta(hours=1)
        )
        outcome = conn.execute(
            "select measurement_status,pnl_bps,context_json from paper_trade_outcomes where trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("valid", outcome["measurement_status"])
        self.assertAlmostEqual(100.0, outcome["pnl_bps"], places=3)
        outcome_context = json.loads(outcome["context_json"])
        self.assertEqual("paired_direct_v1", outcome_context["paper_outcome_measurement_contract"])
        self.assertTrue(
            validate_paired_direct_outcome_provenance(
                paired_candidate(base), outcome_context, settings=cfg
            )["valid"]
        )
        self.assertEqual(2, conn.execute("select count(*) from paper_trade_outcome_price_observations").fetchone()[0])
        self.assertEqual(1, conn.execute("select count(*) from paper_trade_outcome_funding_batches").fetchone()[0])
        self.assertTrue(upgraded[0]["backfilled"])

    def test_due_window_cursor_advances_across_restart_without_exceeding_budget(self) -> None:
        base = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        cfg = bounded_settings([15, 60, 240, 1440])
        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "radar.sqlite"
            conn = storage.connect(db_path)
            insert_bounded_trade(conn, base)
            insert_bounded_trade(conn, base + dt.timedelta(minutes=93))
            first_window_key = None
            seen_horizons: set[int] = set()
            wrapped = False
            for cycle in range(16):
                targets = storage.load_due_paper_outcome_targets(
                    conn, cfg, now=base + dt.timedelta(days=3)
                )
                self.assertTrue(targets)
                start = dt.datetime.fromisoformat(targets[0]["due_window_start_at"])
                end = dt.datetime.fromisoformat(targets[0]["due_window_end_at"])
                span_minutes = int((end - start).total_seconds() / 60)
                self.assertLessEqual(span_minutes + 7, 100)
                if cycle == 0:
                    self.assertEqual(93, span_minutes)
                    first_window_key = targets[0]["due_window_key"]
                elif targets[0]["due_window_key"] == first_window_key:
                    wrapped = True
                seen_horizons.update(int(target["horizon"]) for target in targets)
                self.assertEqual(
                    1,
                    storage.mark_due_paper_outcome_windows_attempted(
                        conn,
                        targets,
                        attempted_at=base + dt.timedelta(days=3, minutes=cycle),
                    ),
                )
                conn.close()
                conn = storage.connect(db_path)
                if wrapped and {15, 60, 240, 1440}.issubset(seen_horizons):
                    break
            self.assertTrue(wrapped)
            self.assertTrue({15, 60, 240, 1440}.issubset(seen_horizons))
            conn.close()

    def test_divergent_paired_leg_cursors_make_progress_and_complete_outcomes(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        cfg = bounded_settings([15, 240], max_delay=300)
        trade_id = insert_bounded_trade(conn, base, candidate=paired_candidate(base))
        now = base + dt.timedelta(minutes=300)

        initial = storage.load_due_paper_outcome_targets(conn, cfg, now=now)
        self.assertEqual(2, len(initial))
        first_perp = next(target for target in initial if target["paired_component"] == "perp")
        self.assertEqual(
            1,
            storage.mark_due_paper_outcome_windows_attempted(
                conn, [first_perp], attempted_at=now
            ),
        )

        divergent = storage.load_due_paper_outcome_targets(conn, cfg, now=now)
        self.assertEqual({"perp", "spot"}, {target["paired_component"] for target in divergent})
        self.assertEqual(2, len({target["parent_outcome_key"] for target in divergent}))
        first_records = []
        for target in divergent:
            target_at = dt.datetime.fromisoformat(str(target["target_at"]))
            first_records.append(
                candle(
                    target_at,
                    99.0 if target["paired_component"] == "perp" else 101.0,
                    venue=str(target["venue"]),
                    inst_id=str(target["inst_id"]),
                    market_surface=str(target["market_surface"]),
                )
            )
        self.assertEqual(
            2, storage.record_paper_price_observations(conn, first_records)["accepted"]
        )
        self.assertEqual(
            2,
            storage.mark_due_paper_outcome_windows_attempted(
                conn, divergent, attempted_at=now + dt.timedelta(minutes=1)
            ),
        )

        complementary = storage.load_due_paper_outcome_targets(conn, cfg, now=now)
        self.assertEqual({"perp", "spot"}, {target["paired_component"] for target in complementary})
        self.assertEqual(2, len({target["parent_outcome_key"] for target in complementary}))
        second_records = []
        for target in complementary:
            target_at = dt.datetime.fromisoformat(str(target["target_at"]))
            second_records.append(
                candle(
                    target_at,
                    99.0 if target["paired_component"] == "perp" else 101.0,
                    venue=str(target["venue"]),
                    inst_id=str(target["inst_id"]),
                    market_surface=str(target["market_surface"]),
                )
            )
        self.assertEqual(
            2, storage.record_paper_price_observations(conn, second_records)["accepted"]
        )
        final_exit = base + dt.timedelta(minutes=241)
        self.assertEqual(
            1,
            storage.record_paper_funding_coverage(
                conn, funding_coverage(base, final_exit)
            )["accepted"],
        )

        recorded = storage.record_due_horizon_outcomes(conn, {}, cfg, now=now)
        self.assertEqual([15, 240], sorted(item["horizon_minutes"] for item in recorded))
        outcomes = conn.execute(
            """
            select horizon_minutes,measurement_status
            from paper_trade_outcomes where trade_id=? order by horizon_minutes
            """,
            (trade_id,),
        ).fetchall()
        self.assertEqual([(15, "valid"), (240, "valid")], [tuple(row) for row in outcomes])
        self.assertEqual(
            4,
            conn.execute(
                "select count(*) from paper_trade_outcome_price_observations"
            ).fetchone()[0],
        )

    def test_retention_never_deletes_an_observation_referenced_by_outcome(self) -> None:
        conn = memory_conn()
        base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        with mock.patch.object(storage, "PAPER_PRICE_OBSERVATION_MAX_ROWS", 2):
            first = storage.record_paper_price_observations(
                conn,
                [
                    candle(base, 100.0, source_event_id="retained"),
                    candle(base + dt.timedelta(minutes=1), 101.0, source_event_id="prunable"),
                ],
            )
            retained_id = first["observation_ids"][0]
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    measurement_status,price_observation_id
                ) values (999,15,?,100,0,'{}','valid',?)
                """,
                (base.isoformat(), retained_id),
            )
            conn.commit()
            result = storage.record_paper_price_observations(
                conn,
                [candle(base + dt.timedelta(minutes=2), 102.0, source_event_id="new")],
            )
        self.assertEqual(1, result["pruned"])
        self.assertIsNotNone(
            conn.execute(
                "select 1 from paper_price_observations where observation_id=?", (retained_id,)
            ).fetchone()
        )
        self.assertEqual(2, conn.execute("select count(*) from paper_price_observations").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
