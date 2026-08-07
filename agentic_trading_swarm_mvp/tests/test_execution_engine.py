from __future__ import annotations

import json
import datetime as dt
import copy
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import build_order_ticket, execute_order  # noqa: E402
from paper_order_router import FRONTIER_PAPER_ADMISSION_REASON_PREFIX  # noqa: E402
from paper_admission_queue import (  # noqa: E402
    enqueue_paper_admission_candidates,
    reconcile_paper_admission_queue,
    select_paper_admission_candidates,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import (  # noqa: E402
    bounded_paper_queue_claim_valid,
    consume_bounded_paper_queue_claim,
    execution_summary,
    has_open_trade,
    init_db,
    open_paper_trade,
    record_due_horizon_outcomes,
    record_paper_price_observations,
    save_execution_order,
    save_opportunity,
)
import strategy_reliability  # noqa: E402


class ExecutionEnginePaperGuardTests(unittest.TestCase):
    @staticmethod
    def bounded_queue_settings() -> dict:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["market_admission"].update(
            {
                "enabled": True,
                "paper_queue_enabled": True,
                "paper_queue": {
                    "max_active": 200,
                    "max_enqueue_per_cycle": 30,
                    "max_select_per_cycle": 30,
                    "max_freshness_age_seconds": 90,
                },
            }
        )
        settings["risk"]["paper_notional_usd"] = 100.0
        return settings

    @staticmethod
    def bounded_candidate() -> dict:
        return {
            "venue": "COINBASE",
            "inst_id": "COINBASE:BTC-USD",
            "asset_class": "crypto",
            "market_type": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 100.0,
            "score": 80.0,
            "edge_bps_estimate": 20.0,
            "gross_edge_bps_estimate": 45.0,
            "estimated_round_trip_cost_bps": 20.0,
            "anomaly_flags": [],
            "quality_status": "verified",
            "quality_action": "normal",
            "freshness_state": "fresh",
            "data_status": "reachable",
            "route_status": "standard",
            "execution_feasibility": {"status": "standard"},
        }

    @staticmethod
    def bounded_review() -> dict:
        return {
            "decision": "approve_paper_trade",
            "learned_score": 80.0,
            "confidence": 0.8,
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 20.0,
            "route_status": "standard",
            "hard_blocks": [],
        }

    def test_bounded_execution_rejects_candidate_without_live_queue_claim(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        result = execute_order(
            conn,
            self.bounded_candidate(),
            self.bounded_review(),
            self.bounded_queue_settings(),
        )
        self.assertFalse(result["paper_filled"])
        self.assertFalse(result["queue_claim_valid"])
        self.assertEqual("blocked_invalid_paper_queue_claim", result["order"]["status"])
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from execution_fills").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trades").fetchone()[0])

    def test_bounded_fill_bundle_rolls_back_if_trade_insert_fails(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(
            conn,
            settings,
            [self.bounded_candidate()],
        )
        claimed = select_paper_admission_candidates(conn, settings)[0]
        with mock.patch("execution_engine.open_paper_trade", side_effect=RuntimeError("forced")):
            with self.assertRaisesRegex(RuntimeError, "forced"):
                execute_order(conn, claimed, self.bounded_review(), settings)
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from execution_fills").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trades").fetchone()[0])

    def test_bounded_fill_atomically_creates_one_idempotent_trade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]
        execution = execute_order(conn, claimed, self.bounded_review(), settings)
        repeated_trade_id = open_paper_trade(
            conn,
            claimed,
            self.bounded_review(),
            execution=execution,
            settings=settings,
        )
        self.assertTrue(execution["paper_filled"])
        self.assertEqual(execution["paper_trade_id"], repeated_trade_id)
        self.assertEqual(1, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertGreater(conn.execute("select count(*) from execution_fills").fetchone()[0], 0)
        self.assertEqual(1, conn.execute("select count(*) from paper_trades").fetchone()[0])

    def test_bounded_fill_uses_quote_event_time_and_first_label_is_fifteen_minutes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        settings["learning"].update(
            {
                "horizon_minutes": [15, 60, 240, 1440],
                "max_outcome_delay_seconds": 300,
            }
        )
        settings["scanner"]["hold_minutes"] = 60
        settings["paper_hold_optimizer"].update(
            {
                "enabled": True,
                "default_hold_minutes": 60,
                "candidate_horizons_minutes": [15, 60, 240, 1440],
            }
        )
        t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)
        candidate = {
            **self.bounded_candidate(),
            "venue": "BYBIT_SPOT",
            "inst_id": "BYBIT_SPOT:BTCUSDT",
            "market_surface": "spot",
            "observed_at": t0.isoformat(),
        }
        enqueue_paper_admission_candidates(
            conn,
            settings,
            [candidate],
            now=(t0 + dt.timedelta(seconds=1)).isoformat(),
        )
        claimed = select_paper_admission_candidates(
            conn,
            settings,
            now=(t0 + dt.timedelta(seconds=1)).isoformat(),
        )[0]

        execution = execute_order(conn, claimed, self.bounded_review(), settings)
        self.assertTrue(execution["paper_filled"])
        trade = conn.execute(
            "select opened_at,context_json from paper_trades where id=?",
            (execution["paper_trade_id"],),
        ).fetchone()
        context = json.loads(trade["context_json"])
        self.assertEqual(t0, dt.datetime.fromisoformat(trade["opened_at"]))
        self.assertEqual(
            "queue_evidence_quote_event_time",
            context["paper_entry_time_semantics"],
        )
        self.assertEqual(t0.isoformat(), context["paper_entry_quote_event_at"])
        self.assertTrue(context["paper_fill_recorded_at"])

        label_at = t0 + dt.timedelta(minutes=15)
        candle_close_at = label_at + dt.timedelta(minutes=1)
        journaled = record_paper_price_observations(
            conn,
            [
                {
                    "source_kind": "exchange_candle_1m_close",
                    "venue": candidate["venue"],
                    "inst_id": candidate["inst_id"],
                    "market_surface": "spot",
                    "candle_open_at": label_at.isoformat(),
                    "event_at": candle_close_at.isoformat(),
                    "received_at": candle_close_at.isoformat(),
                    "price": 101.0,
                    "source_name": "Bybit public REST spot klines",
                    "source_parser": "bybit_v5_1m_klines",
                    "source_endpoint": "/v5/market/kline",
                    "source_event_id": f"test|{label_at.isoformat()}",
                    "is_closed": True,
                    "is_partial": False,
                    "freshness_state": "fresh",
                    "quality_status": "verified",
                }
            ],
        )
        self.assertEqual(1, journaled["accepted"])
        recorded = record_due_horizon_outcomes(
            conn,
            {
                candidate["inst_id"]: {
                    "last": 101.0,
                    "observed_at": label_at.isoformat(),
                    "price_source": "direct_test_quote",
                }
            },
            settings,
            now=candle_close_at,
        )
        self.assertEqual([15], [item["horizon_minutes"] for item in recorded])
        outcome = conn.execute(
            "select horizon_minutes,target_at,observed_at,measurement_status "
            "from paper_trade_outcomes where trade_id=?",
            (execution["paper_trade_id"],),
        ).fetchone()
        self.assertEqual(15, outcome["horizon_minutes"])
        self.assertEqual(label_at, dt.datetime.fromisoformat(outcome["target_at"]))
        self.assertEqual(candle_close_at, dt.datetime.fromisoformat(outcome["observed_at"]))
        self.assertEqual("valid", outcome["measurement_status"])
        self.assertEqual(
            0,
            conn.execute(
                "select count(*) from paper_trade_outcomes where horizon_minutes=5"
            ).fetchone()[0],
        )

    def test_bounded_claim_rejects_stale_future_unparseable_and_mismatched_event_times(self) -> None:
        settings = self.bounded_queue_settings()
        current = dt.datetime.now(dt.timezone.utc)
        for name, observed_at in (
            ("stale", current - dt.timedelta(seconds=91)),
            ("future", current + dt.timedelta(seconds=1)),
        ):
            with self.subTest(case=name):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                init_db(conn)
                candidate = {
                    **self.bounded_candidate(),
                    "observed_at": observed_at.isoformat(),
                }
                enqueue_paper_admission_candidates(
                    conn,
                    settings,
                    [candidate],
                    now=current.isoformat(),
                )
                self.assertEqual(
                    [],
                    select_paper_admission_candidates(
                        conn,
                        settings,
                        now=current.isoformat(),
                    ),
                )
                conn.close()

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            **self.bounded_candidate(),
            "observed_at": current.isoformat(),
        }
        enqueue_paper_admission_candidates(
            conn,
            settings,
            [candidate],
            now=current.isoformat(),
        )
        claimed = select_paper_admission_candidates(
            conn,
            settings,
            now=current.isoformat(),
        )[0]
        mismatched = copy.deepcopy(claimed)
        mismatched["observed_at"] = (
            current - dt.timedelta(seconds=1)
        ).isoformat()
        self.assertFalse(bounded_paper_queue_claim_valid(conn, mismatched, settings))
        self.assertFalse(
            consume_bounded_paper_queue_claim(
                conn,
                mismatched,
                settings,
                now=current.isoformat(),
            )
        )
        conn.execute(
            "update paper_admission_queue set evidence_observed_at='not-a-time'"
        )
        conn.commit()
        self.assertFalse(bounded_paper_queue_claim_valid(conn, claimed, settings))

    def test_consumed_bounded_claim_rejects_second_order_and_trade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]

        execution = execute_order(conn, claimed, self.bounded_review(), settings)
        queue_row = conn.execute(
            """
            select execution_order_id,paper_trade_id
            from paper_admission_queue where queue_id=?
            """,
            (claimed["_paper_admission_queue_id"],),
        ).fetchone()
        self.assertEqual(execution["order_id"], queue_row["execution_order_id"])
        self.assertEqual(execution["paper_trade_id"], queue_row["paper_trade_id"])

        with self.assertRaisesRegex(ValueError, "bounded_paper_execution_order"):
            save_execution_order(
                conn,
                execution["order"],
                claimed,
                self.bounded_review(),
                settings=settings,
            )
        duplicate_execution = dict(execution)
        duplicate_execution["order_id"] = int(execution["order_id"]) + 10_000
        with self.assertRaisesRegex(ValueError, "bounded_paper_trade"):
            open_paper_trade(
                conn,
                claimed,
                self.bounded_review(),
                execution=duplicate_execution,
                settings=settings,
            )

        self.assertEqual(1, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertEqual(1, conn.execute("select count(*) from paper_trades").fetchone()[0])

    def test_bounded_claim_rejects_every_identity_mutation_and_alias_conflict(self) -> None:
        mutations = {
            "venue": lambda item: item.__setitem__("venue", "BYBIT"),
            "instrument": lambda item: item.__setitem__(
                "inst_id", "COINBASE:ETH-USD"
            ),
            "instrument_alias": lambda item: item.__setitem__(
                "instrument_id", "COINBASE:ETH-USD"
            ),
            "surface": lambda item: item.__setitem__(
                "market_surface", "crypto_spot"
            ),
            "surface_alias": lambda item: item.__setitem__(
                "proxy_surface", "crypto_spot"
            ),
            "direction": lambda item: item.__setitem__(
                "direction", "short_frontier_spot"
            ),
            "entry_quote": lambda item: item.__setitem__("last", 101.0),
            "lineage_root_alias": lambda item: item.__setitem__(
                "strategy_lineage_root_id", "foreign-root"
            ),
            "top_level_admission_alias": lambda item: item.__setitem__(
                "paper_admission_key", "foreign-admission"
            ),
            "nested_admission_alias": lambda item: item["paper_admission"].__setitem__(
                "admission_key", "foreign-admission"
            ),
            "episode_alias": lambda item: item["paper_admission"].__setitem__(
                "episode_id", "foreign-episode"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                init_db(conn)
                settings = self.bounded_queue_settings()
                enqueue_paper_admission_candidates(
                    conn, settings, [self.bounded_candidate()]
                )
                claimed = select_paper_admission_candidates(conn, settings)[0]
                mutate(claimed)

                self.assertFalse(
                    bounded_paper_queue_claim_valid(conn, claimed, settings)
                )
                with self.assertRaisesRegex(
                    ValueError, "bounded_paper_opportunity_identity_invalid"
                ):
                    save_opportunity(conn, claimed, self.bounded_review())
                self.assertFalse(
                    consume_bounded_paper_queue_claim(conn, claimed, settings)
                )
                result = execute_order(
                    conn, claimed, self.bounded_review(), settings
                )
                self.assertFalse(result["paper_filled"])
                self.assertEqual(
                    "blocked_invalid_paper_queue_claim", result["order"]["status"]
                )
                self.assertEqual(
                    0, conn.execute("select count(*) from execution_orders").fetchone()[0]
                )
                self.assertEqual(
                    0, conn.execute("select count(*) from paper_trades").fetchone()[0]
                )
                conn.close()

    def test_database_guard_rejects_mutated_claim_snapshot(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]
        self.assertTrue(consume_bounded_paper_queue_claim(conn, claimed, settings))
        mutated = dict(claimed)
        mutated["venue"] = "BYBIT"

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "bounded_paper_execution_order_not_authorized",
        ):
            conn.execute(
                """
                insert into execution_orders(
                    created_at,mode,route_id,venue,inst_id,direction,trade_type,
                    status,notional_usd,order_json,candidate_json,review_json,
                    admission_key,admission_episode_id
                ) values(?, 'paper','direct',?,?,?,?, 'paper_filled',100,'{}',?,'{}',?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    mutated["venue"],
                    mutated["inst_id"],
                    mutated["direction"],
                    mutated["trade_type"],
                    json.dumps(mutated, sort_keys=True),
                    mutated["admission_key"],
                    mutated["episode_id"],
                ),
            )
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

        foreign_lineage = copy.deepcopy(claimed)
        foreign_lineage.update(
            {
                "admission_key": "foreign-admission",
                "paper_admission_key": "foreign-admission",
                "episode_id": "foreign-episode",
                "admission_episode_id": "foreign-episode",
            }
        )
        foreign_lineage["paper_admission"].update(
            {
                "admission_key": "foreign-admission",
                "episode_id": "foreign-episode",
            }
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "bounded_paper_execution_order_not_authorized",
        ):
            conn.execute(
                """
                insert into execution_orders(
                    created_at,mode,route_id,venue,inst_id,direction,trade_type,
                    status,notional_usd,order_json,candidate_json,review_json,
                    admission_key,admission_episode_id
                ) values(?, 'paper','direct',?,?,?,?, 'paper_filled',100,'{}',?,'{}',?,?)
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    foreign_lineage["venue"],
                    foreign_lineage["inst_id"],
                    foreign_lineage["direction"],
                    foreign_lineage["trade_type"],
                    json.dumps(foreign_lineage, sort_keys=True),
                    foreign_lineage["admission_key"],
                    foreign_lineage["episode_id"],
                ),
            )
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])

    def test_capacity_reclaim_rotates_token_without_duplicate_or_corrupt_opportunity(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        settings["market_admission"]["paper_queue"]["retry_backoff_seconds"] = 30
        t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=40)
        first_candidate = {
            **self.bounded_candidate(),
            "observed_at": t0.isoformat(),
        }
        enqueue_paper_admission_candidates(
            conn,
            settings,
            [first_candidate],
            now=t0.isoformat(),
        )
        first_claim = select_paper_admission_candidates(
            conn,
            settings,
            now=(t0 + dt.timedelta(seconds=1)).isoformat(),
        )[0]
        opportunity_id = save_opportunity(
            conn,
            first_claim,
            self.bounded_review(),
        )
        reconcile_paper_admission_queue(
            conn,
            settings,
            now=(t0 + dt.timedelta(seconds=2)).isoformat(),
        )

        refreshed_at = t0 + dt.timedelta(seconds=38)
        refreshed = {
            **first_candidate,
            "last": 101.0,
            "source_timestamp": refreshed_at.isoformat(),
        }
        enqueue_paper_admission_candidates(
            conn,
            settings,
            [refreshed],
            now=refreshed_at.isoformat(),
        )
        second_claim = select_paper_admission_candidates(
            conn,
            settings,
            now=refreshed_at.isoformat(),
        )[0]
        self.assertNotEqual(
            first_claim["_paper_admission_claim_token"],
            second_claim["_paper_admission_claim_token"],
        )
        self.assertEqual(
            opportunity_id,
            save_opportunity(conn, second_claim, self.bounded_review()),
        )
        result = reconcile_paper_admission_queue(
            conn,
            settings,
            now=(refreshed_at + dt.timedelta(seconds=1)).isoformat(),
        )
        self.assertNotIn("opportunity_identity_corrupt", result["by_decision"])
        self.assertEqual(
            1,
            conn.execute("select count(*) from opportunities").fetchone()[0],
        )
        queue_row = conn.execute(
            "select status,opportunity_id,last_reason from paper_admission_queue"
        ).fetchone()
        self.assertEqual("approved_waiting_capacity", queue_row["status"])
        self.assertEqual(opportunity_id, queue_row["opportunity_id"])

    def test_reconcile_rejects_bound_order_whose_fill_status_was_mutated(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]
        execution = execute_order(conn, claimed, self.bounded_review(), settings)
        conn.execute(
            "update execution_orders set status='execution_error' where id=?",
            (execution["order_id"],),
        )
        conn.commit()

        result = reconcile_paper_admission_queue(conn, settings)
        queue_row = conn.execute(
            "select status,execution_order_id,paper_trade_id,last_reason from paper_admission_queue"
        ).fetchone()
        self.assertEqual(1, result["by_decision"]["execution_order_binding_corrupt"])
        self.assertEqual("paper_open", queue_row["status"])
        self.assertEqual(execution["order_id"], queue_row["execution_order_id"])
        self.assertEqual(execution["paper_trade_id"], queue_row["paper_trade_id"])
        self.assertEqual("execution_order_binding_corrupt", queue_row["last_reason"])

    def test_direct_bounded_open_enforces_global_open_trade_capacity_atomically(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        settings["risk"]["max_open_paper_trades"] = 0
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]

        with self.assertRaisesRegex(
            ValueError, "bounded_paper_open_trade_capacity_exhausted"
        ):
            open_paper_trade(
                conn,
                claimed,
                self.bounded_review(),
                settings=settings,
            )

        queue_row = conn.execute(
            "select status,claim_token,execution_order_id,paper_trade_id from paper_admission_queue"
        ).fetchone()
        self.assertEqual("queued_review", queue_row["status"])
        self.assertEqual(
            claimed["_paper_admission_claim_token"], queue_row["claim_token"]
        )
        self.assertIsNone(queue_row["execution_order_id"])
        self.assertIsNone(queue_row["paper_trade_id"])
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trades").fetchone()[0])

    def test_bounded_claim_is_consumed_once_when_execute_is_called_twice(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        settings = self.bounded_queue_settings()
        enqueue_paper_admission_candidates(conn, settings, [self.bounded_candidate()])
        claimed = select_paper_admission_candidates(conn, settings)[0]
        original_token = claimed["_paper_admission_claim_token"]

        first = execute_order(conn, claimed, self.bounded_review(), settings)
        counts_after_first = tuple(
            conn.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("execution_orders", "execution_fills", "paper_trades")
        )
        second = execute_order(conn, claimed, self.bounded_review(), settings)
        counts_after_second = tuple(
            conn.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("execution_orders", "execution_fills", "paper_trades")
        )
        queue_row = conn.execute(
            """
            select status,claim_token,lease_expires_at,consumed_claim_token,
                   claim_consumed_at,attempt_count
            from paper_admission_queue
            """
        ).fetchone()

        self.assertTrue(first["paper_filled"])
        self.assertFalse(second["paper_filled"])
        self.assertFalse(second["queue_claim_valid"])
        self.assertEqual("blocked_invalid_paper_queue_claim", second["order"]["status"])
        self.assertEqual(counts_after_first, counts_after_second)
        self.assertEqual((1, len(first["fill_ids"]), 1), counts_after_second)
        self.assertEqual("paper_open", queue_row["status"])
        self.assertIsNone(queue_row["claim_token"])
        self.assertIsNone(queue_row["lease_expires_at"])
        self.assertEqual(original_token, queue_row["consumed_claim_token"])
        self.assertTrue(queue_row["claim_consumed_at"])
        self.assertEqual(1, queue_row["attempt_count"])

    def test_fill_capacity_deferral_creates_no_fill(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:BTC-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 100.0,
            "score": 80.0,
            "edge_bps_estimate": 12.0,
            "gross_edge_bps_estimate": 35.0,
            "estimated_round_trip_cost_bps": 20.0,
            "anomaly_flags": [],
            "quality_status": "verified",
            "quality_action": "normal",
        }
        review = {
            "decision": "approve_paper_trade",
            "learned_score": 80.0,
            "confidence": 0.8,
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 12.0,
            "feasibility_status": "standard",
            "route_status": "standard",
        }

        result = execute_order(
            conn,
            candidate,
            review,
            DEFAULT_SETTINGS,
            opportunity_id=7,
            allow_paper_fill=False,
        )

        self.assertTrue(result["paper_fill_deferred"])
        self.assertFalse(result["paper_filled"])
        self.assertEqual([], result["fills"])
        self.assertEqual("deferred_capacity", result["order"]["status"])
        self.assertEqual(0, conn.execute("select count(*) from execution_fills").fetchone()[0])
        saved = conn.execute(
            "select opportunity_id,status from execution_orders where id=?", (result["order_id"],)
        ).fetchone()
        self.assertEqual(7, saved["opportunity_id"])
        self.assertEqual("deferred_capacity", saved["status"])

    def test_route_requirement_report_sizes_paper_ticket_without_blocking_it(self) -> None:
        candidate = {
            "venue": "CME_GROUP",
            "inst_id": "CME_GROUP:PROXY",
            "direction": "short_proxy",
            "trade_type": "global_market_discovery_proxy",
            "last": 10.0,
            "paper_route_requirement_report": {
                "paper_only": True,
                "read_only": True,
                "applies": True,
                "paper_allocation_multiplier": 0.6,
                "hard_blocking": False,
            },
        }
        review = {"paper_allocation_multiplier": 1.0}

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual(600.0, ticket["notional_usd"])
        self.assertEqual("ready_for_paper_execution", ticket["status"])

    def test_frontier_ticket_carries_shadow_learning_metadata(self) -> None:
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:ICP-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "last": 10.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": ["simulated_slippage_exceeds_edge"],
            "edge_bps_estimate": 0.0,
            "gross_edge_bps_estimate": 12.898,
            "estimated_round_trip_cost_bps": 20.092,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        review = {
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 0.0,
            "feasibility_status": "standard",
            "route_status": "standard",
        }

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual("shadow_excluded_from_learning", ticket["paper_label_exclusion_reason"])
        self.assertTrue(ticket["paper_shadow_excluded_from_learning"])
        self.assertEqual(
            [
                "simulated_slippage_exceeds_edge",
                "net_edge_after_round_trip_cost_not_positive",
            ],
            ticket["paper_shadow_exclusion_triggers"],
        )
        self.assertEqual(["simulated_slippage_exceeds_edge"], ticket["anomaly_flags"])
        self.assertEqual(0.0, ticket["net_edge_bps_estimate"])

    def test_order_ticket_carries_okx_basis_context_gate_reason(self) -> None:
        candidate = {
            "venue": "OKX",
            "inst_id": "OKX:BTC-USDT-SWAP",
            "direction": "long_perp_short_spot",
            "trade_type": "perp_funding_basis",
            "last": 100.0,
            "paper_context_gate_reason": "okx_reverse_basis_conditional_route_cap",
            "paper_context_gate_action": "cap_conditional_reverse_basis",
            "paper_context_gate_promotion_eligible": False,
            "paper_context_gate_paper_fill_allowed": True,
        }
        review = {
            "paper_allocation_multiplier": 1.0,
            "feasibility_status": "conditional",
            "route_status": "conditional",
        }

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual("okx_reverse_basis_conditional_route_cap", ticket["paper_context_gate_reason"])
        self.assertEqual("cap_conditional_reverse_basis", ticket["paper_context_gate_action"])
        self.assertFalse(ticket["paper_context_gate_promotion_eligible"])
        self.assertTrue(ticket["paper_context_gate_paper_fill_allowed"])

    def test_unconfirmed_frontier_spot_borrow_is_shadow_observed_without_an_order(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "GATE",
            "inst_id": "GATE:ARC_USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "frontier_paper_admission_guard_applies": True,
            "signal_key": "GATE|frontier_crypto_venue_map|short_frontier_spot|conditional",
            "last": 1.0,
            "score": 80.0,
            "edge_bps_estimate": 24.0,
            "gross_edge_bps_estimate": 60.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "execution_route": {
                "route_id": "conditional_crypto_route_paper",
                "route_status": "conditional",
                "missing_permissions": ["spot_borrow"],
                "route_blockers": ["spot_borrow"],
                "borrow_status": "required_unconfirmed",
            },
        }
        review = {
            "decision": "approve_conditional_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 24.0,
            "feasibility_status": "conditional",
            "route_status": "conditional",
            "missing_requirements": ["spot_borrow"],
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(result["paper_filled"])
        self.assertEqual(result["order"]["status"], "shadow_only")
        self.assertEqual([], result["fills"])
        self.assertIsNone(result["order_id"])
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        row = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("short_frontier_spot_spot_borrow_blocked", row["reject_reason"])
        counters = execution_summary(conn)["frontier_paper_candidates"]
        self.assertEqual(0, counters["accepted"])
        self.assertEqual(1, counters["shadowed"])

        deferred_conn = sqlite3.connect(":memory:")
        deferred_conn.row_factory = sqlite3.Row
        init_db(deferred_conn)
        deferred = execute_order(
            deferred_conn,
            candidate,
            review,
            DEFAULT_SETTINGS,
            opportunity_id=42,
            record_shadow_observation=False,
        )
        self.assertTrue(deferred["shadow_observation_deferred"])
        self.assertFalse(deferred["shadow_observation_recorded"])
        self.assertEqual(
            0,
            deferred_conn.execute(
                "select count(*) from frontier_paper_shadow_observations"
            ).fetchone()[0],
        )
        deferred_conn.close()

        observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)
        conn.execute(
            "update frontier_paper_shadow_observations set observed_at = ?",
            (observed_at.isoformat(),),
        )
        outcomes = record_due_horizon_outcomes(
            conn,
            {"GATE:ARC_USDT": {"last": 1.01, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}},
            {"learning": {"horizon_minutes": [60], "max_outcome_delay_seconds": 300}},
        )
        self.assertEqual(1, len(outcomes))
        self.assertIn("shadow_observation_id", outcomes[0])
        self.assertEqual(1, conn.execute("select count(*) from frontier_paper_shadow_outcomes").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trade_outcomes").fetchone()[0])

    def test_execution_summary_counts_accepted_frontier_fill(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:BTC-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 100.0,
            "edge_bps_estimate": 12.0,
            "gross_edge_bps_estimate": 35.0,
            "estimated_round_trip_cost_bps": 20.0,
            "anomaly_flags": [],
            "quality_status": "verified",
            "quality_action": "normal",
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 12.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertTrue(execution["paper_filled"])
        self.assertEqual(15.0, execution["candidate"]["frontier_net_edge_bps"])
        counters = execution_summary(conn)["frontier_paper_candidates"]
        self.assertEqual(1, counters["accepted"])
        self.assertEqual(0, counters["shadowed"])

    def test_execution_and_trade_preserve_opportunity_and_lineage(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:BTC-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "strategy_lab_id": "route_rich_spot_v1__relaxed_r1__relaxed_r2",
            "strategy_lab_version": 3,
            "last": 100.0,
            "score": 82.0,
            "edge_bps_estimate": 12.0,
            "gross_edge_bps_estimate": 35.0,
            "estimated_round_trip_cost_bps": 20.0,
            "anomaly_flags": [],
            "quality_status": "verified",
            "quality_action": "normal",
        }
        review = {
            "decision": "approve_paper_trade",
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 12.0,
            "learned_score": 82.0,
            "confidence": 0.8,
        }

        execution = execute_order(
            conn,
            candidate,
            review,
            DEFAULT_SETTINGS,
            opportunity_id=321,
        )
        trade_id = open_paper_trade(
            conn,
            candidate,
            review,
            execution=execution,
            settings=DEFAULT_SETTINGS,
        )

        order = conn.execute(
            "select opportunity_id, strategy_lineage_root_id from execution_orders where id = ?",
            (execution["order_id"],),
        ).fetchone()
        trade = conn.execute(
            "select opportunity_id, strategy_lineage_root_id from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(321, order["opportunity_id"])
        self.assertEqual("route_rich_spot_v1", order["strategy_lineage_root_id"])
        self.assertEqual(321, trade["opportunity_id"])
        self.assertEqual("route_rich_spot_v1", trade["strategy_lineage_root_id"])
        self.assertTrue(
            has_open_trade(
                conn,
                candidate["inst_id"],
                candidate["direction"],
                strategy_lineage_root="route_rich_spot_v1",
            )
        )
        self.assertFalse(
            has_open_trade(
                conn,
                candidate["inst_id"],
                candidate["direction"],
                strategy_lineage_root="unrelated_lab",
            )
        )
        conn.execute(
            "update paper_trades set strategy_lineage_root_id = null where id = ?",
            (trade_id,),
        )
        conn.commit()
        self.assertTrue(
            has_open_trade(
                conn,
                candidate["inst_id"],
                candidate["direction"],
                strategy_lineage_root="route_rich_spot_v1",
            )
        )
        self.assertFalse(
            has_open_trade(
                conn,
                candidate["inst_id"],
                candidate["direction"],
                strategy_lineage_root="routeXrichXspotXv1",
            )
        )

    def test_frontier_fill_gate_uses_bounded_net_edge_reason(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:ETH-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "frontier_paper_admission_guard_applies": True,
            "last": 100.0,
            "edge_bps_estimate": 0.0,
            "gross_edge_bps_estimate": 20.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": [],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 0.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("net_edge_floor_failed", execution["candidate"]["candidate_reject_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("net_edge_floor_failed", observation["reject_reason"])

    def test_frontier_fill_gate_uses_bounded_shadow_only_quality_reason(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:STRK-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "frontier_paper_admission_guard_applies": True,
            "last": 1.0,
            "score": 95.4,
            "edge_bps_estimate": 16.0,
            "gross_edge_bps_estimate": 30.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "degraded",
            "quality_action": "shadow_only",
            "anomaly_flags": ["depth_cliff"],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 16.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("shadow_only_quality_gate", execution["candidate"]["candidate_reject_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("shadow_only_quality_gate", observation["reject_reason"])

    def test_frontier_fill_gate_persists_specific_shadow_reason_for_invalid_level_value(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:ICP-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 1.0,
            "score": 88.0,
            "edge_bps_estimate": 16.0,
            "gross_edge_bps_estimate": 30.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": ["invalid_level_value"],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 16.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("net_edge_floor_failed", execution["candidate"]["candidate_reject_reason"])
        self.assertEqual("invalid_level_value", execution["candidate"]["shadow_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("invalid_level_value", observation["reject_reason"])

    def test_yahoo_proxy_freshness_shadow_only_candidate_opens_synthetic_research_trade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "seen_at": "2026-08-06T14:19:00+00:00",
            "venue": "YAHOO_PROXY",
            "inst_id": "YAHOO_PROXY:EWZ",
            "direction": "long_proxy",
            "trade_type": "global_proxy_momentum",
            "last": 100.0,
            "score": 88.0,
            "spread_bps": 2.0,
            "liquidity_score": 0.8,
            "change_24h_pct": 1.2,
            "edge_bps_estimate": 10.0,
            "basis_bps": 0.0,
            "funding_bps": 0.0,
            "source_quote_timestamp": "2026-08-06T14:00:00+00:00",
            "source_session_status": "closed",
            "source_session_open": False,
            "source_quote_age_seconds": 1140.0,
            "last_trade_timestamp": "2026-08-06T14:00:00+00:00",
            "last_trade_age_seconds": 1140.0,
            "pre_entry_tick_returns_bps": [-18.0, -10.0, 5.0, -6.0],
            "proxy_reuse_gate": {
                "quote_age_seconds": 1140.0,
                "source_session_status": "closed",
                "reasons": ["opening_gap_without_live_followthrough"],
            },
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        candidate, _ = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"})
        reviewed = candidate[0]
        review = {
            "decision": "approve_paper_trade",
            "signal_key": reviewed["inst_id"],
            "learned_score": reviewed["score"],
            "confidence": 0.8,
            "net_edge_bps_estimate": 10.0,
            "paper_allocation_multiplier": 1.0,
            "feasibility_status": "standard",
            "route_status": "standard",
            "missing_requirements": [],
        }

        execution = execute_order(conn, reviewed, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertTrue(execution["paper_observation_ready"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("synthetic_research", execution["order"]["signal_stats_scope"])

        trade_id = open_paper_trade(conn, reviewed, review, execution=execution, settings=DEFAULT_SETTINGS)
        row = conn.execute(
            "select status, context_json from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        context = json.loads(row["context_json"])
        tags = context["paper_trade_diagnostic_tags"]
        self.assertEqual("open", row["status"])
        self.assertEqual("synthetic_research", context["signal_stats_scope"])
        self.assertEqual("aging_15m_to_60m", tags["quote_staleness_bucket"])
        self.assertEqual("closed", tags["session_bucket"])
        self.assertEqual("intraday_16m_to_60m", tags["selected_holding_horizon_bucket"])

        observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)
        conn.execute("update paper_trades set opened_at = ? where id = ?", (observed_at.isoformat(), trade_id))
        conn.commit()
        recorded = record_due_horizon_outcomes(
            conn,
            {
                "YAHOO_PROXY:EWZ": {
                    "last": 101.0,
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            },
            {"learning": {"horizon_minutes": [60], "max_outcome_delay_seconds": 300}},
        )
        self.assertEqual(1, len(recorded))
        outcome = conn.execute(
            "select context_json from paper_trade_outcomes where trade_id = ?",
            (trade_id,),
        ).fetchone()
        outcome_context = json.loads(outcome["context_json"])
        outcome_tags = outcome_context["paper_trade_diagnostic_tags"]
        self.assertEqual("synthetic_research", outcome_context["signal_stats_scope"])
        self.assertEqual("intraday_16m_to_60m", outcome_tags["outcome_holding_horizon_bucket"])
        self.assertEqual("aging_15m_to_60m", outcome_tags["quote_staleness_bucket"])


if __name__ == "__main__":
    unittest.main()
