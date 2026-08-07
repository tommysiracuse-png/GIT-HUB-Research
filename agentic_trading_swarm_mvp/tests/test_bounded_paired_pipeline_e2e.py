from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import okx_perp_scanner  # noqa: E402
import radar_loop  # noqa: E402
import storage  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from paired_direct_contract import (  # noqa: E402
    CONTRACT_VERSION,
    DECLARED_GROSS_NOTIONAL_USD,
)
from paper_admission_queue import (  # noqa: E402
    enqueue_paper_admission_candidates,
    reconcile_paper_admission_queue,
    select_paper_admission_candidates,
)
from settings import DEFAULT_SETTINGS  # noqa: E402
from strategy_lab import (  # noqa: E402
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    _experiment_outcomes,
)


UTC = dt.timezone.utc


def bounded_settings() -> dict:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["mode"] = "paper"
    settings["allow_live_trading"] = False
    settings.setdefault("operations", {})["fail_closed_recovery_profile"] = True
    settings["risk"].update(
        {
            "paper_notional_usd": DECLARED_GROSS_NOTIONAL_USD,
            "taker_fee_bps_per_leg": 5.0,
            "slippage_bps_per_leg": 3.0,
            "max_open_paper_trades": 100,
        }
    )
    settings["scanner"].update(
        {
            "hold_minutes": 15,
            "max_new_paper_trades": 10,
            "max_new_paper_observations": 20,
        }
    )
    settings["learning"].update(
        {
            "horizon_minutes": [15],
            "max_outcome_delay_seconds": 300,
        }
    )
    settings["paper_hold_optimizer"]["enabled"] = False
    settings["market_admission"].update(
        {
            "enabled": True,
            "monitor_enabled": True,
            "paper_queue_enabled": True,
            "bridge_enabled": False,
            "paper_queue": {
                "max_active": 200,
                "max_enqueue_per_cycle": 30,
                "max_select_per_cycle": 30,
                "selection_lease_seconds": 900,
                "retry_backoff_seconds": 300,
                "max_freshness_age_seconds": 90.0,
                "poor_cohort_min_labels": 20,
                "poor_cohort_max_avg_pnl_bps": -8.0,
                "poor_cohort_max_win_rate": 0.43,
            },
        }
    )
    settings.setdefault("paper_expansion", {})["enabled"] = True
    settings.setdefault("paper_exploration", {})["enabled"] = False
    settings.setdefault("paper_due_outcome_collection", {}).update(
        {
            "enabled": True,
            "paired_max_entry_timestamp_skew_seconds": 2.0,
            "paired_max_exit_timestamp_skew_seconds": 1.0,
            "paired_notional_tolerance_fraction": 0.01,
        }
    )
    return settings


def ticker(inst_id: str, event_at: dt.datetime, *, bid: float, ask: float) -> dict:
    return {
        "instId": inst_id,
        "last": str((bid + ask) / 2.0),
        "bidPx": str(bid),
        "askPx": str(ask),
        "open24h": str((bid + ask) / 2.0 - 1.0),
        "volCcy24h": "10000000",
        "ts": str(int(event_at.timestamp() * 1000)),
    }


def scanner_candidate(decision_time: dt.datetime) -> dict:
    lineage = f"STRATEGY_LAB|{RECOVERY_CANARY_STRATEGY_LAB_ID}|v1"
    candidate = {
        "seen_at": decision_time.isoformat(),
        "venue": "OKX",
        "inst_id": "BTC-USDT-SWAP",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "asset_class": "crypto_linked_derivative",
        "market_surface": "okx_perpetual_swap",
        "trade_type": "perp_funding_basis",
        "direction": "short_perp_long_spot",
        "last": 101.0,
        "index_px": 7.0,
        "score": 90.0,
        "learned_score": 90.0,
        "edge_bps_estimate": 50.0,
        "gross_edge_bps_estimate": 90.0,
        "estimated_round_trip_cost_bps": 32.0,
        "liquidity_score": 0.95,
        "spread_bps": 2.0,
        "quality_score": 95.0,
        "quality_status": "verified",
        "quality_action": "normal",
        "anomaly_flags": [],
        "route_status": "standard",
        "signal_stats_scope": "direct",
        "strategy_lab_id": RECOVERY_CANARY_STRATEGY_LAB_ID,
        "strategy_lab_version": 1,
        "strategy_lab_lineage_root_id": RECOVERY_CANARY_STRATEGY_LAB_ID,
        "signal_lineage_key": lineage,
        "paper_admission": {
            "signal_stats_scope": "direct",
            "strategy_lineage": lineage,
        },
        "execution_route": {
            "route_id": "okx_derivatives_paper",
            "route_status": "standard",
            "missing_permissions": [],
        },
        "execution_feasibility": {
            "status": "standard",
            "route_status": "standard",
            "missing_requirements": [],
        },
    }
    return okx_perp_scanner.apply_paired_direct_entry_contract(
        candidate,
        ticker(
            "BTC-USDT-SWAP",
            decision_time - dt.timedelta(seconds=1),
            bid=100.0,
            ask=100.2,
        ),
        ticker(
            "BTC-USDT",
            decision_time - dt.timedelta(milliseconds=500),
            bid=99.7,
            ask=99.9,
        ),
        bounded_settings(),
        decision_time=decision_time,
    )


def approved_review() -> dict:
    return {
        "decision": "approve_paper_trade",
        "learned_score": 90.0,
        "confidence": 0.9,
        "paper_allocation_multiplier": 1.0,
        "net_edge_bps_estimate": 50.0,
        "route_id": "okx_derivatives_paper",
        "effective_route_id": "okx_derivatives_paper",
        "route_status": "standard",
        "feasibility_status": "standard",
        "missing_requirements": [],
        "hard_blocks": [],
    }


def closed_candle(
    candle_open_at: dt.datetime,
    price: float,
    *,
    venue: str,
    inst_id: str,
    market_surface: str,
) -> dict:
    event_at = candle_open_at + dt.timedelta(minutes=1)
    return {
        "source_kind": "exchange_candle_1m_close",
        "venue": venue,
        "inst_id": inst_id,
        "market_surface": market_surface,
        "candle_open_at": candle_open_at.isoformat(),
        "event_at": event_at.isoformat(),
        "received_at": event_at.isoformat(),
        "price": price,
        "source_name": "OKX public REST history candles",
        "source_parser": "okx_1m_candles",
        "source_endpoint": "/api/v5/market/history-candles",
        "source_event_id": f"{venue}|{inst_id}|{candle_open_at.isoformat()}",
        "is_closed": True,
        "is_partial": False,
        "freshness_state": "fresh",
        "quality_status": "verified",
    }


def realized_funding_coverage(entry_at: dt.datetime, exit_at: dt.datetime) -> dict:
    request_url = (
        "https://www.okx.com/api/v5/public/funding-rate-history?"
        "instId=BTC-USDT-SWAP&limit=400"
    )
    received_at = exit_at + dt.timedelta(seconds=1)
    payload_sha256 = hashlib.sha256(b"bounded-pipeline-funding-response").hexdigest()
    query_identity = {
        "request_url": request_url,
        "requested_from": entry_at.isoformat(),
        "requested_through": exit_at.isoformat(),
        "received_at": received_at.isoformat(),
        "payload_sha256": payload_sha256,
    }
    query_id = "okx-funding-query-" + hashlib.sha256(
        json.dumps(
            query_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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


class BoundedPairedPipelineEndToEndTests(unittest.TestCase):
    def test_exact_okx_pair_reaches_one_reliable_label_and_corrupt_pairs_do_not(self) -> None:
        settings = bounded_settings()
        decision_time = dt.datetime.now(UTC)
        candidate = scanner_candidate(decision_time)
        review = approved_review()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            enqueue = enqueue_paper_admission_candidates(
                conn,
                settings,
                [candidate],
                now=decision_time.isoformat(),
            )
            self.assertEqual(1, enqueue["active_enqueued"], enqueue)
            claimed = select_paper_admission_candidates(
                conn,
                settings,
                now=decision_time.isoformat(),
                limit=1,
            )
            self.assertEqual(1, len(claimed))
            queued_candidate = claimed[0]
            self.assertEqual("evidence", queued_candidate["_paper_admission_lane"])
            admission_key = queued_candidate["admission_key"]
            episode_id = queued_candidate["episode_id"]

            pending_review = {
                **review,
                "decision": "pending_execution",
                "intended_decision": review["decision"],
                "execution_status": "pending",
            }
            opportunity_id = storage.save_opportunity(
                conn,
                queued_candidate,
                pending_review,
            )
            execution = execute_order(
                conn,
                queued_candidate,
                review,
                settings,
                opportunity_id=opportunity_id,
            )
            conn.commit()
            self.assertTrue(execution["paper_filled"], execution)
            self.assertTrue(execution["queue_claim_valid"], execution)
            self.assertEqual(2, len(execution["fills"]))
            self.assertEqual(
                [("OKX", "BTC-USDT-SWAP", "sell"), ("OKX_SPOT", "BTC-USDT", "buy")],
                [
                    (fill["venue"], fill["inst_id"], fill["side"])
                    for fill in execution["fills"]
                ],
            )
            self.assertEqual(
                [50.0, 50.0],
                [fill["notional_usd"] for fill in execution["fills"]],
            )
            self.assertEqual(
                2,
                conn.execute("select count(*) from execution_fills").fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute("select count(*) from paper_trades").fetchone()[0],
            )
            storage.update_opportunity_decision(
                conn,
                opportunity_id,
                "paper_filled",
                {
                    **review,
                    "decision": "paper_filled",
                    "intended_decision": review["decision"],
                    "execution_status": "paper_filled",
                    "execution_order_id": execution["order_id"],
                    "paper_trade_id": execution["paper_trade_id"],
                },
            )
            reconcile_paper_admission_queue(conn, settings)
            self.assertEqual(
                "paper_open",
                conn.execute(
                    "select status from paper_admission_queue where admission_key=? and episode_id=?",
                    (admission_key, episode_id),
                ).fetchone()[0],
            )

            trade = conn.execute(
                "select * from paper_trades where id=?",
                (execution["paper_trade_id"],),
            ).fetchone()
            opened_at = dt.datetime.fromisoformat(trade["opened_at"])
            simulated_now = opened_at + dt.timedelta(minutes=20)
            real_loader = storage.load_due_paper_outcome_targets
            observed_targets: list[dict] = []

            def load_at_simulated_time(
                connection: sqlite3.Connection,
                effective_settings: dict,
                *,
                limit: int = 1_000,
            ) -> list[dict]:
                return real_loader(
                    connection,
                    effective_settings,
                    now=simulated_now,
                    limit=limit,
                )

            def no_network_collector(provider, *, settings):
                del settings
                targets = provider.load_due_instruments(limit=100)
                observed_targets.extend(targets)
                self.assertEqual({"perp", "spot"}, {row["paired_component"] for row in targets})
                self.assertEqual(1, len({row["parent_outcome_key"] for row in targets}))
                self.assertEqual(2, len({row["outcome_key"] for row in targets}))
                target_at = dt.datetime.fromisoformat(targets[0]["target_at"])
                exit_at = target_at + dt.timedelta(minutes=1)
                return {
                    "enabled": True,
                    "records": [
                        closed_candle(
                            target_at,
                            99.0,
                            venue="OKX",
                            inst_id="BTC-USDT-SWAP",
                            market_surface="perpetual_swap",
                        ),
                        closed_candle(
                            target_at,
                            101.0,
                            venue="OKX_SPOT",
                            inst_id="BTC-USDT",
                            market_surface="spot",
                        ),
                    ],
                    "funding_events": [],
                    "funding_coverage": [
                        realized_funding_coverage(opened_at, exit_at)
                    ],
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
                    "limits": {"max_instruments": 100, "max_workers": 1},
                }

            with mock.patch.object(
                radar_loop,
                "load_due_paper_outcome_targets",
                side_effect=load_at_simulated_time,
            ):
                collection = radar_loop._collect_and_persist_due_outcomes(
                    conn,
                    settings,
                    collector=no_network_collector,
                )
            self.assertEqual("persisted", collection["status"], collection)
            self.assertEqual(2, collection["price_persistence"]["accepted"])
            self.assertEqual(1, collection["funding_persistence"]["accepted"])
            self.assertEqual(2, len(observed_targets))
            self.assertEqual(
                2,
                conn.execute("select count(*) from paper_price_observations").fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute(
                    "select count(*) from paper_funding_coverage_batches"
                ).fetchone()[0],
            )

            recorded = storage.record_due_horizon_outcomes(
                conn,
                {},
                settings,
                now=simulated_now + dt.timedelta(minutes=1),
            )
            self.assertEqual(1, len(recorded), recorded)
            self.assertEqual("valid", recorded[0]["measurement_status"])
            self.assertIsNotNone(recorded[0]["funding_coverage_batch_id"])
            closed = storage.close_due_trades(conn, {}, 15, settings)
            self.assertEqual(1, len(closed), closed)
            self.assertEqual("valid", closed[0]["measurement_status"])

            reconciliation = reconcile_paper_admission_queue(
                conn,
                settings,
                now=(simulated_now + dt.timedelta(minutes=2)).isoformat(),
            )
            self.assertEqual(1, reconciliation["by_decision"]["reliable_valid_outcome"])
            queue_row = conn.execute(
                "select * from paper_admission_queue where admission_key=? and episode_id=?",
                (admission_key, episode_id),
            ).fetchone()
            self.assertEqual("completed_valid", queue_row["status"])
            self.assertEqual(execution["order_id"], queue_row["execution_order_id"])
            self.assertEqual(execution["paper_trade_id"], queue_row["paper_trade_id"])
            self.assertEqual(
                1,
                conn.execute(
                    """
                    select count(*) from market_admission_transitions
                    where admission_key=? and episode_id=?
                      and to_stage='queue:completed_valid'
                    """,
                    (admission_key, episode_id),
                ).fetchone()[0],
            )

            lineage = conn.execute(
                """
                select q.admission_key as queue_key,q.episode_id as queue_episode,
                       p.admission_key as opportunity_key,
                       p.admission_episode_id as opportunity_episode,
                       e.admission_key as order_key,
                       e.admission_episode_id as order_episode,
                       t.admission_key as trade_key,
                       t.admission_episode_id as trade_episode,
                       o.admission_key as outcome_key,
                       o.admission_episode_id as outcome_episode
                from paper_admission_queue q
                join opportunities p on p.id=q.opportunity_id
                join execution_orders e on e.id=q.execution_order_id
                join paper_trades t on t.id=q.paper_trade_id
                join paper_trade_outcomes o
                  on o.trade_id=t.id and o.horizon_minutes=t.selected_hold_minutes
                where q.queue_id=?
                """,
                (queue_row["queue_id"],),
            ).fetchone()
            self.assertEqual(
                {admission_key},
                {
                    lineage["queue_key"],
                    lineage["opportunity_key"],
                    lineage["order_key"],
                    lineage["trade_key"],
                    lineage["outcome_key"],
                },
            )
            self.assertEqual(
                {episode_id},
                {
                    lineage["queue_episode"],
                    lineage["opportunity_episode"],
                    lineage["order_episode"],
                    lineage["trade_episode"],
                    lineage["outcome_episode"],
                },
            )

            outcome_row = conn.execute(
                """
                select p.status,p.signal_key,p.pnl_bps,p.direction,
                       p.candidate_json,p.review_json,p.context_json,
                       p.close_measurement_status,
                       o.context_json as outcome_context_json,
                       o.measurement_status as selected_outcome_measurement_status,
                       o.pnl_bps as selected_outcome_pnl_bps
                from paper_trades p
                join paper_trade_outcomes o
                  on o.trade_id=p.id and o.horizon_minutes=p.selected_hold_minutes
                where p.id=?
                """,
                (execution["paper_trade_id"],),
            ).fetchone()
            reliable = storage.reliable_paper_label_eligibility_for_trade_row(
                outcome_row
            )
            self.assertTrue(reliable["paper_label_eligible"], reliable)

            aggregate = storage.performance_summary(conn)
            self.assertEqual(1, aggregate["closed"], aggregate)
            self.assertAlmostEqual(
                float(outcome_row["pnl_bps"]),
                aggregate["avg_pnl_bps"],
                places=3,
            )
            lab = _experiment_outcomes(
                conn,
                RECOVERY_CANARY_STRATEGY_LAB_ID,
                15,
                experiment={
                    "version": 1,
                    "source_surface": "okx_perpetual_swap",
                    "permitted_target_surface": ["okx_perpetual_swap"],
                },
                settings=settings,
            )
            self.assertEqual(1, lab["valid_count"], lab)
            self.assertEqual(1, lab["metrics"]["count"], lab)
            self.assertAlmostEqual(
                float(outcome_row["pnl_bps"]),
                lab["metrics"]["avg_pnl_bps"],
                places=3,
            )
            self.assertEqual(0, lab["realized_cost_backfill"]["applied_count"])

            valid_row = dict(outcome_row)
            tampered_row = {
                **valid_row,
                "pnl_bps": float(valid_row["pnl_bps"]) + 1.0,
            }
            tampered = storage.reliable_paper_label_eligibility_for_trade_row(
                tampered_row
            )
            self.assertFalse(tampered["paper_label_eligible"], tampered)
            self.assertIn("row.pnl_bps_mismatch", tampered["paired_direct_outcome_reasons"])

            legacy_candidate = json.loads(valid_row["candidate_json"])
            legacy_candidate.pop(CONTRACT_VERSION, None)
            legacy = storage.reliable_paper_label_eligibility_for_trade_row(
                {
                    **valid_row,
                    "candidate_json": json.dumps(legacy_candidate, sort_keys=True),
                }
            )
            self.assertFalse(legacy["paper_label_eligible"], legacy)
            self.assertIn(
                "entry.paired_direct_v1_missing",
                legacy["paired_direct_outcome_reasons"],
            )

            missing_funding_context = json.loads(valid_row["outcome_context_json"])
            missing_funding_context["paired_direct_v1_outcome"].pop(
                "funding_coverage",
                None,
            )
            missing_funding = storage.reliable_paper_label_eligibility_for_trade_row(
                {
                    **valid_row,
                    "outcome_context_json": json.dumps(
                        missing_funding_context,
                        sort_keys=True,
                    ),
                }
            )
            self.assertFalse(
                missing_funding["paper_label_eligible"],
                missing_funding,
            )
            self.assertIn(
                "outcome.funding_coverage.batch_id",
                missing_funding["paired_direct_outcome_reasons"],
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
