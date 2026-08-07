import datetime as dt
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

import paper_admission_queue as admission_queue
import paper_expansion_campaign as campaign
import radar_loop
from storage import init_db, open_paper_trade, save_opportunity


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    init_db(conn)
    return conn


def queue_settings():
    return {
        "market_admission": {
            "enabled": True,
            "paper_queue_enabled": True,
            "paper_queue": {
                "max_active": 200,
                "max_enqueue_per_cycle": 30,
                "max_select_per_cycle": 30,
            },
        },
        "scanner": {"hold_minutes": 60},
        "risk": {
            "paper_notional_usd": 100.0,
            "taker_fee_bps_per_leg": 0.0,
            "slippage_bps_per_leg": 0.0,
        },
        "learning": {"horizon_minutes": [60], "max_outcome_delay_seconds": 300.0},
    }


def direct_candidate():
    return {
        "venue": "OKX",
        "inst_id": "BTC-USDT-SWAP",
        "asset_class": "crypto",
        "market_type": "perp",
        "trade_type": "perp_funding_basis",
        # These campaign-accounting fixtures exercise exact lineage, not the
        # paired basis contract.  Use a directly measurable single-perp label.
        "direction": "funding_capture_short_perp",
        "quality_status": "verified",
        "freshness_state": "fresh",
        "data_status": "reachable",
        "route_status": "standard",
        "execution_feasibility": {"status": "standard"},
        "last": 100.0,
        "score": 80.0,
        "signal_lineage_key": "OKX|perp_funding_basis|direct",
        "thesis": "bounded exact-attribution test",
    }


class BoundedRadarIntegrationTests(unittest.TestCase):
    def test_database_growth_uses_logical_pages_for_start_and_finalization_with_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "wal-footprint.sqlite"
            conn = sqlite3.connect(path)
            try:
                self.assertEqual("wal", conn.execute("pragma journal_mode=wal").fetchone()[0])
                conn.execute("pragma wal_autocheckpoint=0")
                conn.execute("create table footprint_probe (payload blob)")
                conn.commit()
                conn.execute(
                    "insert into footprint_probe(payload) values (?)",
                    (b"a" * 100_000,),
                )
                conn.commit()

                cycle_start = radar_loop._database_logical_footprint(path)
                self.assertEqual(
                    radar_loop._sqlite_logical_footprint_bytes(conn),
                    cycle_start,
                )
                # WAL is intentionally non-empty, so main+WAL is a different
                # (diagnostic-only) unit and must not become the gate baseline.
                self.assertGreater(
                    radar_loop._database_storage_footprint(path),
                    cycle_start,
                )

                conn.execute(
                    "insert into footprint_probe(payload) values (?)",
                    (b"b" * 100_000,),
                )
                final_uncommitted = campaign._sqlite_logical_footprint_bytes(conn)
                self.assertEqual(
                    radar_loop._sqlite_logical_footprint_bytes(conn),
                    final_uncommitted,
                )
                self.assertGreater(final_uncommitted, cycle_start)
            finally:
                conn.rollback()
                conn.close()

    def test_lane_caps_are_exactly_half_and_bias_single_canary_to_evidence(self):
        self.assertEqual({"evidence": 5, "discovery": 5}, radar_loop._paper_lane_limits(10))
        self.assertEqual({"evidence": 10, "discovery": 10}, radar_loop._paper_lane_limits(20))
        self.assertEqual({"evidence": 1, "discovery": 0}, radar_loop._paper_lane_limits(1))
        self.assertEqual({"evidence": 0, "discovery": 0}, radar_loop._paper_lane_limits(0))

    def test_nonqueued_approval_is_terminal_and_never_persisted_as_approved(self):
        persisted = radar_loop._not_queued_execution_review(
            {"decision": "approve_paper_trade", "hard_blocks": []}
        )
        self.assertEqual("reviewed_not_queued", persisted["decision"])
        self.assertEqual("approve_paper_trade", persisted["intended_decision"])
        self.assertEqual(
            "not_selected_for_bounded_paper_queue", persisted["execution_status"]
        )

    def test_accounting_excludes_nonqueued_approval_but_keeps_filled_intent(self):
        approval = {"decision": "approve_paper_trade", "hard_blocks": []}
        rows = radar_loop._reviewed_for_accounting(
            [
                {
                    "candidate": {"inst_id": "BTC-USDT-SWAP"},
                    "review": approval,
                    "execution_review": radar_loop._not_queued_execution_review(approval),
                },
                {
                    "candidate": {"inst_id": "ETH-USDT-SWAP"},
                    "review": approval,
                    "execution_review": {"decision": "paper_filled"},
                },
            ]
        )
        self.assertEqual("reviewed_not_queued", rows[0]["review"]["decision"])
        self.assertEqual("approve_paper_trade", rows[1]["review"]["decision"])

    def test_campaign_metrics_use_exact_queue_lineage(self):
        now = dt.datetime.now(dt.timezone.utc)
        started_at = (now - dt.timedelta(minutes=1)).isoformat()
        settings = queue_settings()
        with memory_db() as conn:
            admission_queue.enqueue_paper_admission_candidates(
                conn, settings, [direct_candidate()], now=started_at
            )
            candidate = admission_queue.select_paper_admission_candidates(
                conn, settings, now=now.isoformat()
            )[0]
            review = {
                "decision": "approve_paper_trade",
                "learned_score": 80.0,
                "confidence": 0.8,
                "hard_blocks": [],
                "route_status": "standard",
            }
            opportunity_id = save_opportunity(conn, candidate, review)
            order = {
                "mode": "paper",
                "route_id": "okx_public_paper",
                "status": "paper_filled",
                "notional_usd": 100.0,
            }
            trade_id = open_paper_trade(
                conn,
                candidate,
                review,
                execution={
                    "candidate": candidate,
                    "opportunity_id": opportunity_id,
                    "order": order,
                    "fills": [{"fill_price": 100.0, "fee_bps": 0.0, "slippage_bps": 0.0}],
                },
                settings=settings,
            )
            conn.execute(
                """
                update paper_trades
                set status='closed',closed_at=?,pnl_bps=12.0,
                    close_measurement_status='valid'
                where id=?
                """,
                (now.isoformat(), trade_id),
            )
            conn.execute(
                """
                insert into paper_trade_outcomes(
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    measurement_status,admission_key,admission_episode_id
                ) values(?,60,?,101.0,12.0,'{}','valid',?,?)
                """,
                (
                    trade_id,
                    now.isoformat(),
                    candidate["admission_key"],
                    candidate["episode_id"],
                ),
            )
            conn.execute(
                """
                insert into market_admission_transitions(
                    admission_key,episode_id,occurred_at,from_stage,to_stage,
                    transition_kind,details_json
                ) values(?,?,?,'paper_eligible','paper_evaluated','advanced','{}')
                """,
                (candidate["admission_key"], candidate["episode_id"], now.isoformat()),
            )
            conn.commit()
            admission_queue.reconcile_paper_admission_queue(
                conn,
                settings,
                now=now.isoformat(),
            )
            cycle = {
                "enabled": True,
                "cycle_started_at": started_at,
                "phase_started_at": started_at,
                "campaign_config": {"health": {"max_artifact_bytes": {}}},
            }
            reviewed = [
                {
                    "candidate": candidate,
                    "review": review,
                    "execution_review": {"decision": "paper_filled"},
                }
            ]
            with mock.patch.object(radar_loop, "DB_PATH", ROOT / "does-not-exist.sqlite"):
                metrics = radar_loop._bounded_campaign_metrics(
                    conn,
                    cycle,
                    settings=settings,
                    reviewed=reviewed,
                    frontier_crypto_venues={
                        "summary": {"observation_count": 6000, "reachable_venue_count": 16}
                    },
                    runtime_seconds=12.0,
                    db_size_before=0,
                )

            self.assertEqual(1, metrics["new_exact_attributed_admission_keys_paper_evaluated"])
            self.assertEqual(1, metrics["new_direct_closes"])
            self.assertEqual(1, metrics["new_reliable_direct_closes"])
            self.assertEqual(1, metrics["new_timely_direct_closes"])
            self.assertEqual(1, metrics["new_horizon_outcomes"])
            self.assertEqual(1, metrics["new_timely_horizon_outcomes"])
            self.assertEqual(1.0, metrics["opportunity_lineage_coverage"])
            self.assertEqual(1.0, metrics["execution_order_lineage_coverage"])
            self.assertEqual(1.0, metrics["paper_trade_lineage_coverage"])
            self.assertEqual(0, metrics["new_synthetic_proxy_primary"])
            self.assertEqual(0, metrics["lineage_corruption_count"])

    def test_campaign_close_and_horizon_rates_only_use_phase_exact_primary_cohort(self):
        now = dt.datetime.now(dt.timezone.utc)
        phase_started = now - dt.timedelta(hours=2)
        cycle_started = now - dt.timedelta(hours=1)
        settings = queue_settings()
        names = (
            "valid",
            "late",
            "unresolved",
            "old_phase",
            "legacy_scope",
            "mislinked",
            "outcome_mismatch",
            "early_horizon",
            "outcome_without_close",
            "missing_due",
            "not_due",
        )
        candidates = [
            {
                **direct_candidate(),
                "inst_id": f"{name.upper()}-USDT-SWAP",
                "signal_lineage_key": f"OKX|perp_funding_basis|{name}",
            }
            for name in names
        ]
        with memory_db() as conn:
            admission_queue.enqueue_paper_admission_candidates(
                conn,
                settings,
                candidates,
                now=now.isoformat(),
            )
            selected_candidates = {
                item["inst_id"]: item
                for item in admission_queue.select_paper_admission_candidates(
                    conn,
                    settings,
                    now=now.isoformat(),
                )
            }
            queue_rows = {
                row["inst_id"]: row
                for row in conn.execute(
                    "select * from paper_admission_queue order by inst_id"
                ).fetchall()
            }
            trade_ids = {}
            for name in names:
                inst_id = f"{name.upper()}-USDT-SWAP"
                queue_row = queue_rows[inst_id]
                opened_at = (
                    phase_started - dt.timedelta(minutes=1)
                    if name == "old_phase"
                    else now - dt.timedelta(minutes=30)
                    if name == "not_due"
                    else now - dt.timedelta(minutes=61)
                    if name == "outcome_without_close"
                    else phase_started + dt.timedelta(minutes=10)
                    if name in {"early_horizon", "missing_due"}
                    else cycle_started + dt.timedelta(minutes=1)
                )
                context = {"signal_stats_scope": "direct", "route_status": "standard"}
                close_status = "valid"
                if name == "late":
                    close_status = "late"
                elif name == "unresolved":
                    context.update(
                        {
                            "route_status": "conditional",
                            "route_blockers": ["missing_route_requirement"],
                        }
                    )
                elif name == "legacy_scope":
                    context = {"route_status": "standard"}
                trade_id = open_paper_trade(
                    conn,
                    selected_candidates[inst_id],
                    {
                        "decision": "approve_paper_trade",
                        "learned_score": 80.0,
                        "confidence": 0.8,
                        "hard_blocks": [],
                        "route_status": "standard",
                    },
                    settings=settings,
                )
                if name in {"missing_due", "not_due", "outcome_without_close"}:
                    conn.execute(
                        "update paper_trades set opened_at=? where id=?",
                        (opened_at.isoformat(), trade_id),
                    )
                    trade_ids[name] = trade_id
                    if name == "outcome_without_close":
                        conn.execute(
                            """
                            insert into paper_trade_outcomes(
                                trade_id,horizon_minutes,measured_at,price,pnl_bps,
                                context_json,measurement_status,admission_key,
                                admission_episode_id
                            ) values(?,60,?,101.0,10.0,'{}','valid',?,?)
                            """,
                            (
                                trade_id,
                                now.isoformat(),
                                queue_row["admission_key"],
                                queue_row["episode_id"],
                            ),
                        )
                    continue
                conn.execute(
                    """
                    update paper_trades
                    set opened_at=?,closed_at=?,exit=101.0,pnl_bps=10.0,
                        status='closed',context_json=?,close_measurement_status=?
                    where id=?
                    """,
                    (
                        opened_at.isoformat(),
                        now.isoformat(),
                        json.dumps(context),
                        close_status,
                        trade_id,
                    ),
                )
                trade_ids[name] = trade_id
                outcome_episode = (
                    "wrong-episode"
                    if name == "outcome_mismatch"
                    else queue_row["episode_id"]
                )
                conn.execute(
                    """
                    insert into paper_trade_outcomes(
                        trade_id,horizon_minutes,measured_at,price,pnl_bps,
                        context_json,measurement_status,admission_key,
                        admission_episode_id
                    ) values(?,60,?,101.0,10.0,'{}',?,?,?)
                    """,
                    (
                        trade_id,
                        (
                            cycle_started - dt.timedelta(minutes=30)
                            if name == "early_horizon"
                            else now
                        ).isoformat(),
                        "late" if name == "late" else "valid",
                        queue_row["admission_key"],
                        outcome_episode,
                    ),
                )
            conn.execute(
                "update paper_trades set admission_episode_id='wrong-episode' where id=?",
                (trade_ids["mislinked"],),
            )
            conn.commit()

            cycle = {
                "enabled": True,
                "cycle_started_at": cycle_started.isoformat(),
                "phase_started_at": phase_started.isoformat(),
                "campaign_config": {"health": {"max_artifact_bytes": {}}},
            }
            with mock.patch.object(radar_loop, "DB_PATH", ROOT / "does-not-exist.sqlite"):
                metrics = radar_loop._bounded_campaign_metrics(
                    conn,
                    cycle,
                    settings=settings,
                    reviewed=[],
                    frontier_crypto_venues={
                        "summary": {"observation_count": 6000, "reachable_venue_count": 16}
                    },
                    runtime_seconds=1.0,
                    db_size_before=0,
                    captured_at=now.isoformat(),
                )

            self.assertEqual(5, metrics["new_direct_closes"])
            self.assertEqual(3, metrics["new_reliable_direct_closes"])
            self.assertEqual(3, metrics["new_timely_direct_closes"])
            self.assertEqual(4, metrics["new_horizon_outcomes"])
            self.assertEqual(1, metrics["new_timely_horizon_outcomes"])
            self.assertEqual(7, metrics["phase_due_direct_closes"])
            self.assertEqual(3, metrics["phase_reliable_direct_closes"])
            self.assertEqual(3, metrics["phase_timely_direct_closes"])
            self.assertEqual(7, metrics["phase_due_horizon_outcomes"])
            self.assertEqual(2, metrics["phase_timely_horizon_outcomes"])

    def test_lineage_coverage_keeps_cross_episode_mismatch_in_denominator(self):
        now = dt.datetime.now(dt.timezone.utc)
        started_at = (now - dt.timedelta(minutes=1)).isoformat()
        settings = queue_settings()
        first = direct_candidate()
        second = {**direct_candidate(), "inst_id": "ETH-USDT-SWAP"}
        with memory_db() as conn:
            admission_queue.enqueue_paper_admission_candidates(
                conn, settings, [first, second], now=started_at
            )
            episodes = conn.execute(
                "select admission_key,episode_id from paper_admission_queue order by queue_id"
            ).fetchall()
            corrupted = {
                **direct_candidate(),
                "seen_at": now.isoformat(),
                "admission_key": episodes[0]["admission_key"],
                "episode_id": episodes[1]["episode_id"],
                "admission_episode_id": episodes[1]["episode_id"],
            }
            save_opportunity(
                conn,
                corrupted,
                {"decision": "reject", "learned_score": 80.0},
            )
            save_opportunity(
                conn,
                direct_candidate(),
                {
                    "decision": "reviewed_not_queued",
                    "execution_status": "not_selected_for_bounded_paper_queue",
                    "learned_score": 80.0,
                },
            )
            total, coverage = radar_loop._lineage_coverage(
                conn,
                "opportunities",
                "seen_at",
                started_at,
                extra_where="and artifact.decision <> 'reviewed_not_queued'",
            )
            self.assertEqual(1, total)
            self.assertEqual(0.0, coverage)

    def test_exact_evaluated_count_excludes_null_and_cross_episode_transitions(self):
        now = dt.datetime.now(dt.timezone.utc)
        started_at = (now - dt.timedelta(minutes=1)).isoformat()
        settings = queue_settings()
        first = direct_candidate()
        second = {**direct_candidate(), "inst_id": "ETH-USDT-SWAP"}
        with memory_db() as conn:
            admission_queue.enqueue_paper_admission_candidates(
                conn, settings, [first, second], now=started_at
            )
            episodes = conn.execute(
                "select admission_key,episode_id from paper_admission_queue order by queue_id"
            ).fetchall()
            conn.executemany(
                """
                insert into market_admission_transitions(
                    admission_key,episode_id,occurred_at,from_stage,to_stage,
                    transition_kind,details_json
                ) values(?,?,?,'paper_eligible','paper_evaluated','advanced','{}')
                """,
                [
                    (episodes[0]["admission_key"], None, now.isoformat()),
                    (
                        episodes[0]["admission_key"],
                        episodes[1]["episode_id"],
                        now.isoformat(),
                    ),
                ],
            )
            conn.commit()
            cycle = {
                "enabled": True,
                "cycle_started_at": started_at,
                "phase_started_at": started_at,
                "campaign_config": {"health": {"max_artifact_bytes": {}}},
            }
            with mock.patch.object(radar_loop, "DB_PATH", ROOT / "does-not-exist.sqlite"):
                metrics = radar_loop._bounded_campaign_metrics(
                    conn,
                    cycle,
                    settings=settings,
                    reviewed=[],
                    frontier_crypto_venues={
                        "summary": {"observation_count": 6000, "reachable_venue_count": 16}
                    },
                    runtime_seconds=1.0,
                    db_size_before=0,
                )
            self.assertEqual(0, metrics["new_exact_attributed_admission_keys_paper_evaluated"])


if __name__ == "__main__":
    unittest.main()
