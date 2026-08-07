import datetime as dt
import contextlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_admission
import paper_admission_queue as queue
from storage import (
    init_db,
    open_paper_trade,
    record_due_horizon_outcomes,
    record_paper_price_observations,
    save_execution_order,
    save_frontier_paper_shadow_observation,
    save_opportunity,
    signal_key,
)
from strategy_lab import (
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    _recovery_canary_direct_admission_lineage_eligible,
)


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    init_db(conn)
    return conn


def settings(**queue_overrides):
    return {
        "market_admission": {
            "enabled": True,
            "paper_queue_enabled": True,
            "paper_queue": {
                "max_active": 200,
                "max_enqueue_per_cycle": 30,
                "max_select_per_cycle": 4,
                "selection_lease_seconds": 30,
                "retry_backoff_seconds": 30,
                **queue_overrides,
            },
        },
        "scanner": {"hold_minutes": 60},
        "risk": {"taker_fee_bps_per_leg": 0.0, "slippage_bps_per_leg": 0.0},
        "learning": {"horizon_minutes": [1], "max_outcome_delay_seconds": 300.0},
    }


def candidate(index=0, **overrides):
    item = {
        "venue": "KRAKEN",
        "inst_id": f"ASSET-{index}-USD",
        "asset_class": "crypto",
        "market_type": "spot",
        "trade_type": "crypto_spot",
        "direction": "long_frontier_spot",
        "quality_status": "verified",
        "freshness_state": "fresh",
        "data_status": "reachable",
        "route_status": "standard",
        "execution_feasibility": {"status": "standard"},
        "last": 100.0 + index,
        "score": 70.0 + index,
        "signal_lineage_key": f"lineage-{index}",
        "thesis": "bounded direct paper measurement",
    }
    item.update(overrides)
    return item


def review(decision="approve_paper_trade"):
    return {"decision": decision, "learned_score": 70.0, "hard_blocks": []}


class PaperAdmissionQueueTests(unittest.TestCase):
    def test_recovery_admission_requires_a_qualified_outcome_measurement_path(self):
        recovery = settings()
        recovery["operations"] = {"fail_closed_recovery_profile": True}
        supported = candidate(
            200,
            venue="GATE",
            inst_id="BTC_USDT",
            market_surface="frontier_crypto_venue_map",
            trade_type="frontier_crypto_venue_map",
        )
        admitted = queue.classify_paper_admission_candidate(supported, recovery)
        self.assertTrue(admitted["eligible"], admitted)
        self.assertTrue(admitted["outcome_measurement_capability"]["capable"])

        unsupported = queue.classify_paper_admission_candidate(candidate(201), recovery)
        self.assertFalse(unsupported["eligible"])
        self.assertEqual("synthetic_shadow_only", unsupported["queue_status"])
        self.assertEqual(
            "outcome_measurement_capability_unavailable", unsupported["reason"]
        )
        self.assertFalse(unsupported["outcome_measurement_capability"]["capable"])

    def test_recovery_admission_never_promotes_legacy_one_leg_basis_proxy(self):
        recovery = settings()
        recovery["operations"] = {"fail_closed_recovery_profile": True}
        legacy = candidate(
            202,
            venue="OKX",
            inst_id="BTC-USDT-SWAP",
            market_surface="okx_perpetual_swap",
            trade_type="perp_funding_basis",
            market_type="perp",
            direction="short_perp_long_spot",
            paper_label_eligible=True,
        )
        classification = queue.classify_paper_admission_candidate(legacy, recovery)
        self.assertFalse(classification["eligible"])
        self.assertEqual("synthetic_shadow_only", classification["queue_status"])
        self.assertEqual(
            "paired_direct_contract_invalid_or_incomplete", classification["reason"]
        )

    def test_direction_is_versioned_into_identity_and_opposing_episodes_do_not_collide(self):
        long_candidate = candidate(
            90,
            inst_id="COLLISION-USD",
            signal_lineage_key="shared-directional-lineage",
            direction="long_frontier_spot",
        )
        short_candidate = {
            **long_candidate,
            "direction": "short_frontier_spot",
            "score": 71.0,
        }
        long_key = market_admission.admission_key_for(long_candidate)
        short_key = market_admission.admission_key_for(short_candidate)
        self.assertNotEqual(long_key, short_key)
        self.assertEqual(
            long_key,
            market_admission.admission_key_for(
                {**long_candidate, "direction": " LONG-FRONTIER SPOT "}
            ),
        )
        long_audit = market_admission.admission_identity_audit_for(long_candidate)
        short_audit = market_admission.admission_identity_audit_for(short_candidate)
        self.assertEqual(2, long_audit["identity_version"])
        self.assertEqual(
            long_audit["legacy_v1_admission_key"],
            short_audit["legacy_v1_admission_key"],
        )

        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [long_candidate, short_candidate],
                now="2026-08-07T12:00:00+00:00",
            )
            rows = conn.execute(
                "select admission_key,episode_id,candidate_json from paper_admission_queue"
            ).fetchall()
            self.assertEqual(2, result["enqueued"])
            self.assertEqual(2, len({row["admission_key"] for row in rows}))
            self.assertEqual(2, len({row["episode_id"] for row in rows}))
            self.assertEqual(
                2,
                conn.execute("select count(*) from market_admission_states").fetchone()[0],
            )
            self.assertTrue(
                all(
                    json.loads(row["candidate_json"])["paper_admission"][
                        "identity_version"
                    ]
                    == 2
                    for row in rows
                )
            )

    def test_fk_safe_enqueue_dedupe_and_lease_are_separate_from_status(self):
        item = candidate(
            venue="OKX",
            inst_id="BTC-USDT-SWAP",
            trade_type="perp_funding_basis",
            market_type="perp",
            direction="short_perp_long_spot",
        )
        with memory_db() as conn:
            created = queue.enqueue_paper_admission_candidates(
                conn, settings(), [item], now="2026-08-07T12:00:00+00:00"
            )
            self.assertEqual(1, created["enqueued"])
            row = conn.execute("select * from paper_admission_queue").fetchone()
            self.assertEqual("queued_review", row["status"])
            self.assertEqual(0, row["attempt_count"])
            self.assertIsNotNone(
                conn.execute(
                    "select 1 from market_admission_states where admission_key=?",
                    (row["admission_key"],),
                ).fetchone()
            )

            duplicate = queue.enqueue_paper_admission_candidates(
                conn, settings(), [item], now="2026-08-07T12:00:01+00:00"
            )
            self.assertEqual(0, duplicate["enqueued"])
            self.assertEqual(1, duplicate["by_result"]["deduplicated"])
            row = conn.execute("select * from paper_admission_queue").fetchone()
            self.assertEqual(0, row["attempt_count"])
            self.assertEqual(1, row["dedupe_count"])

            selected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:00:02+00:00"
            )
            self.assertEqual(1, len(selected))
            self.assertEqual("evidence", selected[0]["_paper_admission_lane"])
            self.assertTrue(selected[0]["_paper_admission_queue_id"])
            self.assertTrue(selected[0]["admission_key"])
            self.assertTrue(selected[0]["episode_id"])
            row = conn.execute("select * from paper_admission_queue").fetchone()
            self.assertEqual("queued_review", row["status"])
            self.assertEqual(1, row["attempt_count"])
            self.assertTrue(row["claim_token"])
            self.assertEqual(
                [],
                queue.select_paper_admission_candidates(
                    conn, settings(), now="2026-08-07T12:00:03+00:00"
                ),
            )

            self.assertEqual(
                1,
                queue.release_expired_paper_admission_leases(
                    conn, now="2026-08-07T12:01:00+00:00"
                ),
            )
            row = conn.execute("select * from paper_admission_queue").fetchone()
            self.assertEqual("queued_review", row["status"])
            self.assertIsNone(row["claim_token"])

    def test_classifier_is_strict_and_routes_synthetic_to_shadow_only(self):
        cfg = settings()
        self.assertFalse(
            queue.classify_paper_admission_candidate(
                candidate(quality_status="verified_proxy"), cfg
            )["eligible"]
        )
        self.assertFalse(
            queue.classify_paper_admission_candidate(
                candidate(route_status="conditional"), cfg
            )["eligible"]
        )
        self.assertFalse(
            queue.classify_paper_admission_candidate(
                candidate(freshness_state="stale"), cfg
            )["eligible"]
        )
        self.assertFalse(
            queue.classify_paper_admission_candidate(
                candidate(
                    asset_class="equity",
                    market_type="equity",
                    trade_type="equity",
                    venue="NYSE",
                ),
                cfg,
            )["eligible"]
        )
        synthetic = queue.classify_paper_admission_candidate(
            candidate(route_status="paper_testable_research"), cfg
        )
        self.assertEqual("synthetic_shadow_only", synthetic["queue_status"])

        bybit = queue.classify_paper_admission_candidate(
            candidate(
                venue="BYBIT_SPOT",
                direction="long_frontier_spot",
                route_status="feasible",
            ),
            cfg,
        )
        self.assertTrue(bybit["eligible"])
        self.assertEqual("evidence", bybit["lane"])

    def test_foreign_admission_key_cannot_refresh_another_candidate(self):
        item_a = candidate(65)
        key_a = market_admission.admission_key_for(item_a)
        item_a.update(
            {
                "admission_key": key_a,
                "paper_admission": {"admission_key": key_a},
            }
        )
        item_b = candidate(
            66,
            admission_key=key_a,
            paper_admission={"admission_key": key_a},
        )
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(conn, settings(), [item_a])
            rejected = queue.enqueue_paper_admission_candidates(conn, settings(), [item_b])
            row = conn.execute(
                "select candidate_json,dedupe_count from paper_admission_queue"
            ).fetchone()
        self.assertEqual(0, rejected["enqueued"])
        self.assertEqual(1, rejected["by_result"]["admission_identity_mismatch"])
        self.assertEqual(item_a["inst_id"], json.loads(row["candidate_json"])["inst_id"])
        self.assertEqual(0, row["dedupe_count"])

    def test_select_limit_leases_only_returned_rows_and_tags_claim_token(self):
        items = [candidate(index) for index in range(70, 74)]
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn, settings(), items, now="2026-08-07T12:00:00+00:00"
            )
            first = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now="2026-08-07T12:00:01+00:00",
                limit=1,
            )
            self.assertEqual(1, len(first))
            selected = first[0]
            token = selected["_paper_admission_claim_token"]
            self.assertTrue(token)
            self.assertEqual(token, selected["paper_admission_claim_token"])
            self.assertEqual(token, selected["paper_admission"]["claim_token"])
            self.assertEqual(
                selected["_paper_admission_queue_id"],
                selected["paper_admission"]["queue_id"],
            )
            rows = conn.execute(
                "select queue_id,claim_token from paper_admission_queue"
            ).fetchall()
            leased = [row for row in rows if row["claim_token"]]
            self.assertEqual(1, len(leased))
            self.assertEqual(token, leased[0]["claim_token"])

            second = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now="2026-08-07T12:00:02+00:00",
                limit=1,
            )
            self.assertEqual(1, len(second))
            self.assertNotEqual(
                selected["_paper_admission_queue_id"],
                second[0]["_paper_admission_queue_id"],
            )

    def test_stale_snapshot_is_terminalized_before_claim(self):
        cfg = settings(max_freshness_age_seconds=90)
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn,
                cfg,
                [candidate(75)],
                now="2026-08-07T12:00:00+00:00",
            )
            selected = queue.select_paper_admission_candidates(
                conn,
                cfg,
                now="2026-08-07T12:01:31+00:00",
            )
            row = conn.execute(
                "select status,attempt_count,claim_token,last_reason from paper_admission_queue"
            ).fetchone()
        self.assertEqual([], selected)
        self.assertEqual("terminal_reject", row["status"])
        self.assertEqual("stale_before_review_claim", row["last_reason"])
        self.assertEqual(0, row["attempt_count"])
        self.assertIsNone(row["claim_token"])

    def test_selection_is_half_evidence_half_discovery_and_venue_balanced(self):
        items = []
        for index in range(4):
            items.append(
                candidate(
                    index,
                    venue="OKX",
                    inst_id=f"COIN-{index}-USDT-SWAP",
                    market_type="perp",
                    trade_type="perp_funding_basis",
                    direction="short_perp_long_spot",
                )
            )
        for index, venue in enumerate(("KRAKEN", "COINBASE", "KRAKEN", "COINBASE"), 10):
            items.append(candidate(index, venue=venue))
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn, settings(), items, now="2026-08-07T12:00:00+00:00"
            )
            self.assertEqual(8, result["enqueued"])
            selected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:00:01+00:00"
            )
            self.assertEqual({"evidence": 2, "discovery": 2}, Counter(
                item["_paper_admission_lane"] for item in selected
            ))
            discovery_venues = {
                item["venue"] for item in selected if item["_paper_admission_lane"] == "discovery"
            }
            self.assertEqual({"KRAKEN", "COINBASE"}, discovery_venues)

    def test_measurement_probe_requires_one_named_historical_guard(self):
        no_guard = candidate(1)
        guarded = candidate(
            2,
            candidate_reject_detail={"guard": "strategy_reliability"},
        )
        lineage_guarded = candidate(
            4,
            candidate_reject_reason="lineage_source_negative_edge",
            paper_lineage_source_health={"action": "quarantine"},
        )
        hard_blocked = candidate(
            3,
            candidate_reject_reason="strategy_reliability cost threshold",
        )
        mixed_safety_blocked = candidate(
            5,
            candidate_reject_reason="paper_entry_blocked_by_safety_gate",
            candidate_reject_detail={"guard": "strategy_reliability"},
        )
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [
                    no_guard,
                    guarded,
                    lineage_guarded,
                    hard_blocked,
                    mixed_safety_blocked,
                ],
                now="2026-08-07T12:00:00+00:00",
            )
            self.assertEqual(3, result["enqueued"])
            self.assertEqual(
                2,
                result["by_result"]["explicit_quality_route_cost_or_capability_block"],
            )
            selected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:00:01+00:00"
            )
            selected_by_inst = {item["inst_id"]: item for item in selected}
            self.assertFalse(
                selected_by_inst[no_guard["inst_id"]]["_paper_measurement_probe_allowed"]
            )
            self.assertIsNone(
                selected_by_inst[no_guard["inst_id"]]["_paper_measurement_probe_guard"]
            )
            self.assertTrue(
                selected_by_inst[guarded["inst_id"]]["_paper_measurement_probe_allowed"]
            )
            self.assertEqual(
                "strategy_reliability",
                selected_by_inst[guarded["inst_id"]]["_paper_measurement_probe_guard"],
            )
            self.assertTrue(
                selected_by_inst[lineage_guarded["inst_id"]][
                    "_paper_measurement_probe_allowed"
                ]
            )
            self.assertEqual(
                "paper_lineage_source_health",
                selected_by_inst[lineage_guarded["inst_id"]][
                    "_paper_measurement_probe_guard"
                ],
            )

    def test_reliable_poor_discovery_cohort_is_excluded(self):
        item = candidate(5)
        stats = {
            signal_key(item): {
                "reliable_labels": 20,
                "avg_pnl_bps": -8.0,
                "win_rate": 0.9,
            }
        }
        with memory_db() as conn, mock.patch.object(queue, "_cohort_stats", return_value=stats):
            result = queue.enqueue_paper_admission_candidates(conn, settings(), [item])
            self.assertEqual(0, result["enqueued"])
            self.assertEqual(1, result["by_result"]["poor_discovery_cohort"])

    def test_mature_historical_loser_is_terminal_even_without_campaign_labels(self):
        item = candidate(
            51,
            candidate_reject_reason="known_losing_cohort",
            candidate_reject_detail={"guard": "strategy_reliability"},
            bounded_historical_cohort={
                "status": "ready",
                "scope": "all_reliable_direct_paper_history",
                "reliable_labels": 20,
                "avg_pnl_bps": -8.0,
                "win_rate": 0.9,
                "minimum_labels": 20,
                "known_losing_cohort": True,
            },
        )
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [item],
                now="2026-08-07T12:00:00+00:00",
            )
            selected = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now="2026-08-07T12:00:01+00:00",
            )
            row = conn.execute(
                "select status,eligibility_json from paper_admission_queue"
            ).fetchone()

        self.assertEqual(1, result["terminal_audit_enqueued"])
        self.assertEqual("terminal_reject", row["status"])
        self.assertEqual(
            "known_losing_cohort",
            json.loads(row["eligibility_json"])["reason"],
        )
        self.assertEqual([], selected)

    def test_historical_quarantine_probe_requires_historical_under_sampling(self):
        base = candidate(
            52,
            candidate_reject_detail={"guard": "strategy_reliability"},
        )
        under_sampled = {
            **base,
            "bounded_historical_cohort": {
                "status": "ready",
                "reliable_labels": 3,
                "avg_pnl_bps": -2.0,
                "win_rate": 0.5,
                "minimum_labels": 20,
                "known_losing_cohort": False,
            },
        }
        mature = {
            **base,
            "inst_id": "MATURE-HISTORY-USD",
            "signal_lineage_key": "mature-history-lineage",
            "bounded_historical_cohort": {
                "status": "ready",
                "reliable_labels": 20,
                "avg_pnl_bps": 1.0,
                "win_rate": 0.55,
                "minimum_labels": 20,
                "known_losing_cohort": False,
            },
        }
        exact_under_sampled = {
            signal_key(under_sampled): {
                "reliable_labels": 0,
                "avg_pnl_bps": None,
                "win_rate": None,
            },
            signal_key(mature): {
                "reliable_labels": 0,
                "avg_pnl_bps": None,
                "win_rate": None,
            },
        }
        with memory_db() as conn, mock.patch.object(
            queue, "_cohort_stats", return_value=exact_under_sampled
        ):
            queue.enqueue_paper_admission_candidates(
                conn,
                settings(max_select_per_cycle=4),
                [under_sampled, mature],
                now="2026-08-07T12:00:00+00:00",
            )
            selected = queue.select_paper_admission_candidates(
                conn,
                settings(max_select_per_cycle=4),
                now="2026-08-07T12:00:01+00:00",
            )

        by_inst = {item["inst_id"]: item for item in selected}
        self.assertTrue(
            by_inst[under_sampled["inst_id"]]["_paper_measurement_probe_allowed"]
        )
        self.assertFalse(
            by_inst[mature["inst_id"]]["_paper_measurement_probe_allowed"]
        )

    def test_active_probe_refresh_recomputes_mature_cohort_and_terminalizes(self):
        item = candidate(
            6,
            candidate_reject_detail={"guard": "strategy_reliability"},
        )
        under_sampled = {
            signal_key(item): {
                "reliable_labels": 3,
                "avg_pnl_bps": -2.0,
                "win_rate": 0.5,
            }
        }
        mature_poor = {
            signal_key(item): {
                "reliable_labels": 20,
                "avg_pnl_bps": -8.0,
                "win_rate": 0.8,
            }
        }
        with memory_db() as conn:
            with mock.patch.object(queue, "_cohort_stats", return_value=under_sampled):
                queue.enqueue_paper_admission_candidates(
                    conn, settings(), [item], now="2026-08-07T12:00:00+00:00"
                )
            initial = conn.execute("select * from paper_admission_queue").fetchone()
            self.assertTrue(
                json.loads(initial["eligibility_json"])["measurement_probe_allowed"]
            )

            with mock.patch.object(queue, "_cohort_stats", return_value=mature_poor):
                refreshed = queue.enqueue_paper_admission_candidates(
                    conn, settings(), [item], now="2026-08-07T12:00:10+00:00"
                )
                selected = queue.select_paper_admission_candidates(
                    conn, settings(), now="2026-08-07T12:00:11+00:00"
                )

            row = conn.execute("select * from paper_admission_queue").fetchone()
            eligibility = json.loads(row["eligibility_json"])
            self.assertEqual(
                1,
                refreshed["by_result"]["poor_discovery_cohort_terminalized"],
            )
            self.assertEqual("terminal_reject", row["status"])
            self.assertEqual("poor_discovery_cohort", row["last_reason"])
            self.assertEqual(20, eligibility["reliable_labels"])
            self.assertFalse(eligibility["measurement_probe_allowed"])
            self.assertEqual(0, row["attempt_count"])
            self.assertIsNone(row["claim_token"])
            self.assertEqual([], selected)

    def test_terminal_refresh_preserves_inflight_artifact_until_valid_completion(self):
        scenarios = (
            ("paper_open", {"route_status": "paper_testable_research", "shadow_only": True}),
            ("waiting_outcome", {"reference_only": True}),
        )
        for index, (protected_status, terminal_fields) in enumerate(scenarios, start=1):
            with self.subTest(protected_status=protected_status), memory_db() as conn:
                started = dt.datetime.now(dt.timezone.utc)
                stamp = lambda seconds: (started + dt.timedelta(seconds=seconds)).isoformat()
                item = candidate(600 + index)
                queue.enqueue_paper_admission_candidates(
                    conn,
                    settings(),
                    [item],
                    now=stamp(0),
                )
                selected = queue.select_paper_admission_candidates(
                    conn,
                    settings(),
                    now=stamp(1),
                )[0]
                trade_id = open_paper_trade(
                    conn,
                    selected,
                    review(),
                    settings=settings(),
                )
                queue.reconcile_paper_admission_queue(
                    conn,
                    settings(),
                    now=stamp(2),
                )
                if protected_status == "waiting_outcome":
                    conn.execute(
                        """
                        update paper_trades
                        set status='closed',closed_at=?,exit=101.0,pnl_bps=10.0,
                            close_measurement_status='valid'
                        where id=?
                        """,
                        (stamp(3), trade_id),
                    )
                    conn.commit()
                    queue.reconcile_paper_admission_queue(
                        conn,
                        settings(),
                        now=stamp(4),
                    )
                before = conn.execute(
                    "select status,paper_trade_id,candidate_json from paper_admission_queue"
                ).fetchone()
                self.assertEqual(protected_status, before["status"])
                terminal_refresh = {
                    **selected,
                    **terminal_fields,
                    "last": float(selected["last"]) + 1.0,
                    "source_timestamp": stamp(5),
                }
                if terminal_fields.get("shadow_only"):
                    terminal_refresh["execution_feasibility"] = {
                        "status": "paper_testable_research"
                    }
                refreshed = queue.enqueue_paper_admission_candidates(
                    conn,
                    settings(),
                    [terminal_refresh],
                    now=stamp(5),
                )
                after = conn.execute(
                    "select status,paper_trade_id,candidate_json from paper_admission_queue"
                ).fetchone()
                self.assertEqual(
                    1,
                    refreshed["by_result"][
                        "artifact_inflight_terminal_refresh_preserved"
                    ],
                )
                self.assertEqual(protected_status, after["status"])
                self.assertEqual(trade_id, after["paper_trade_id"])
                self.assertEqual(before["candidate_json"], after["candidate_json"])

                conn.execute(
                    """
                    update paper_trades
                    set status='closed',closed_at=?,exit=101.0,pnl_bps=10.0,
                        close_measurement_status='valid'
                    where id=?
                    """,
                    (stamp(6), trade_id),
                )
                conn.execute(
                    """
                    insert into paper_trade_outcomes(
                        trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                        measurement_status,admission_key,admission_episode_id
                    ) values(?,1,?,101.0,10.0,'{}','valid',?,?)
                    """,
                    (
                        trade_id,
                        stamp(6),
                        selected["admission_key"],
                        selected["episode_id"],
                    ),
                )
                conn.commit()
                queue.reconcile_paper_admission_queue(
                    conn,
                    settings(),
                    now=stamp(7),
                )
                completed = conn.execute(
                    "select status,last_reason from paper_admission_queue"
                ).fetchone()
                self.assertEqual("completed_valid", completed["status"])
                self.assertEqual("reliable_valid_outcome", completed["last_reason"])

    def test_selection_rechecks_cohort_before_claim_without_attempt_churn(self):
        item = candidate(
            7,
            candidate_reject_detail={"guard": "paper_strategy_family_quarantine"},
        )
        under_sampled = {
            signal_key(item): {
                "reliable_labels": 2,
                "avg_pnl_bps": None,
                "win_rate": None,
            }
        }
        mature_poor = {
            signal_key(item): {
                "reliable_labels": 20,
                "avg_pnl_bps": 1.0,
                "win_rate": 0.43,
            }
        }
        with memory_db() as conn:
            with mock.patch.object(queue, "_cohort_stats", return_value=under_sampled):
                queue.enqueue_paper_admission_candidates(
                    conn, settings(), [item], now="2026-08-07T12:00:00+00:00"
                )
            with mock.patch.object(queue, "_cohort_stats", return_value=mature_poor):
                selected = queue.select_paper_admission_candidates(
                    conn, settings(), now="2026-08-07T12:00:10+00:00"
                )
            row = conn.execute("select * from paper_admission_queue").fetchone()
            eligibility = json.loads(row["eligibility_json"])
            self.assertEqual([], selected)
            self.assertEqual("terminal_reject", row["status"])
            self.assertEqual("poor_discovery_cohort_before_claim", row["last_reason"])
            self.assertEqual(20, eligibility["reliable_labels"])
            self.assertFalse(eligibility["measurement_probe_allowed"])
            self.assertEqual(0, row["attempt_count"])
            self.assertEqual(0, row["selection_count"])

    def test_fresh_adverse_route_refresh_terminalizes_old_selectable_snapshot(self):
        item = candidate(8)
        adverse = {
            **item,
            "route_status": "unavailable",
            "execution_feasibility": {"status": "unavailable"},
            "freshness_state": "fresh",
        }
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn, settings(), [item], now="2026-08-07T12:00:00+00:00"
            )
            refreshed = queue.enqueue_paper_admission_candidates(
                conn, settings(), [adverse], now="2026-08-07T12:00:10+00:00"
            )
            selected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:00:11+00:00"
            )
            row = conn.execute("select * from paper_admission_queue").fetchone()
            stored_candidate = json.loads(row["candidate_json"])
            self.assertEqual(
                1,
                refreshed["by_result"]["fresh_invalidating_evidence_terminalized"],
            )
            self.assertEqual("terminal_reject", row["status"])
            self.assertEqual(
                "fresh_invalidating_evidence:route_unavailable", row["last_reason"]
            )
            self.assertEqual("unavailable", stored_candidate["route_status"])
            self.assertEqual(0, row["attempt_count"])
            self.assertEqual([], selected)

    def test_fresh_zero_price_and_watch_only_refreshes_retire_old_snapshots(self):
        priced = candidate(81)
        directional = candidate(82)
        zero_price = {**priced, "last": 0.0, "freshness_state": "fresh"}
        watch_only = {
            **directional,
            "direction": "watch_only",
            "freshness_state": "fresh",
        }
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [priced, directional],
                now="2026-08-07T12:00:00+00:00",
            )
            result = queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [zero_price, watch_only],
                now="2026-08-07T12:00:10+00:00",
            )
            rows = {
                row["inst_id"]: row
                for row in conn.execute(
                    "select inst_id,status,last_reason,attempt_count from paper_admission_queue"
                )
            }
            self.assertEqual(
                2, result["by_result"]["fresh_invalidating_evidence_terminalized"]
            )
            self.assertEqual("terminal_reject", rows[priced["inst_id"]]["status"])
            self.assertEqual(
                "fresh_invalidating_evidence:price_missing",
                rows[priced["inst_id"]]["last_reason"],
            )
            self.assertEqual("terminal_reject", rows[directional["inst_id"]]["status"])
            self.assertEqual(
                "fresh_invalidating_evidence:direction_missing",
                rows[directional["inst_id"]]["last_reason"],
            )
            self.assertTrue(all(row["attempt_count"] == 0 for row in rows.values()))
            self.assertEqual(
                [],
                queue.select_paper_admission_candidates(
                    conn, settings(), now="2026-08-07T12:00:11+00:00"
                ),
            )

    def test_zero_fill_capacity_never_reclaims_approved_row_or_changes_artifacts(self):
        item = candidate(9)
        no_fill_slots = {"evidence": 0, "discovery": 0}
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn, settings(), [item], now="2026-08-07T12:00:00+00:00"
            )
            first = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now="2026-08-07T12:00:01+00:00",
                paper_fill_slots_by_lane=no_fill_slots,
            )
            self.assertEqual(1, len(first))
            opportunity_id = save_opportunity(conn, first[0], review())
            queue.reconcile_paper_admission_queue(
                conn, settings(), now="2026-08-07T12:00:02+00:00"
            )
            before = dict(conn.execute("select * from paper_admission_queue").fetchone())
            self.assertEqual("approved_waiting_capacity", before["status"])

            for second in (3, 4):
                self.assertEqual(
                    [],
                    queue.select_paper_admission_candidates(
                        conn,
                        settings(),
                        now=f"2026-08-07T12:00:0{second}+00:00",
                        paper_fill_slots_by_lane=no_fill_slots,
                    ),
                )
            after = dict(conn.execute("select * from paper_admission_queue").fetchone())
            for field in (
                "status",
                "attempt_count",
                "selection_count",
                "opportunity_id",
                "execution_order_id",
                "paper_trade_id",
                "claim_token",
                "selected_at",
                "updated_at",
            ):
                self.assertEqual(before[field], after[field], field)
            self.assertEqual(opportunity_id, after["opportunity_id"])
            self.assertEqual(
                0, conn.execute("select count(*) from execution_orders").fetchone()[0]
            )

    def test_stale_capacity_wait_survives_later_cycles_and_database_restart(self):
        item = candidate(91)
        fill_slots = {"evidence": 1, "discovery": 1}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "radar.sqlite"
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma foreign_keys=on")
                init_db(conn)
                queue.enqueue_paper_admission_candidates(
                    conn,
                    settings(max_freshness_age_seconds=90),
                    [item],
                    now="2026-08-07T12:00:00+00:00",
                )
                selected = queue.select_paper_admission_candidates(
                    conn,
                    settings(max_freshness_age_seconds=90),
                    now="2026-08-07T12:00:01+00:00",
                )
                self.assertEqual(1, len(selected))
                save_opportunity(conn, selected[0], review())
                queue.reconcile_paper_admission_queue(
                    conn,
                    settings(max_freshness_age_seconds=90),
                    now="2026-08-07T12:00:02+00:00",
                )
                before = dict(
                    conn.execute("select * from paper_admission_queue").fetchone()
                )
                self.assertEqual("approved_waiting_capacity", before["status"])

            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma foreign_keys=on")
                for current_cycle in (
                    "2026-08-07T12:15:00+00:00",
                    "2026-08-07T12:30:00+00:00",
                ):
                    self.assertEqual(
                        [],
                        queue.select_paper_admission_candidates(
                            conn,
                            settings(max_freshness_age_seconds=90),
                            now=current_cycle,
                            paper_fill_slots_by_lane=fill_slots,
                        ),
                    )
                after = dict(
                    conn.execute("select * from paper_admission_queue").fetchone()
                )

        self.assertEqual("approved_waiting_capacity", after["status"])
        for field in (
            "queue_id",
            "episode_id",
            "evidence_fingerprint",
            "attempt_count",
            "selection_count",
            "opportunity_id",
            "updated_at",
            "last_reason",
        ):
            self.assertEqual(before[field], after[field], field)
        self.assertIsNone(after["claim_token"])

    def test_fresh_evidence_reactivates_same_capacity_waiting_episode(self):
        item = candidate(92)
        cfg = settings(max_freshness_age_seconds=90)
        fill_slots = {"evidence": 1, "discovery": 1}
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn, cfg, [item], now="2026-08-07T12:00:00+00:00"
            )
            initial = queue.select_paper_admission_candidates(
                conn, cfg, now="2026-08-07T12:00:01+00:00"
            )[0]
            save_opportunity(conn, initial, review())
            queue.reconcile_paper_admission_queue(
                conn, cfg, now="2026-08-07T12:00:02+00:00"
            )
            waiting = dict(
                conn.execute("select * from paper_admission_queue").fetchone()
            )
            self.assertEqual(
                [],
                queue.select_paper_admission_candidates(
                    conn,
                    cfg,
                    now="2026-08-07T12:15:00+00:00",
                    paper_fill_slots_by_lane=fill_slots,
                ),
            )

            refreshed = {
                **item,
                "last": float(item["last"]) + 0.25,
                "source_timestamp": "2026-08-07T12:15:01+00:00",
            }
            refresh_result = queue.enqueue_paper_admission_candidates(
                conn,
                cfg,
                [refreshed],
                now="2026-08-07T12:15:01+00:00",
            )
            refreshed_row = dict(
                conn.execute("select * from paper_admission_queue").fetchone()
            )
            selected = queue.select_paper_admission_candidates(
                conn,
                cfg,
                now="2026-08-07T12:15:02+00:00",
                paper_fill_slots_by_lane=fill_slots,
            )

        self.assertEqual(1, refresh_result["by_result"]["active_refreshed"])
        self.assertEqual("approved_waiting_capacity", refreshed_row["status"])
        self.assertEqual(waiting["queue_id"], refreshed_row["queue_id"])
        self.assertEqual(waiting["episode_id"], refreshed_row["episode_id"])
        self.assertNotEqual(
            waiting["evidence_fingerprint"], refreshed_row["evidence_fingerprint"]
        )
        self.assertEqual(1, len(selected))
        self.assertEqual(
            waiting["queue_id"], selected[0]["_paper_admission_queue_id"]
        )
        self.assertEqual(waiting["episode_id"], selected[0]["episode_id"])

    def test_reconcile_uses_exact_external_statuses(self):
        items = [candidate(index) for index in range(20, 24)]
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(
                conn, settings(), items, now="2026-08-07T12:00:00+00:00"
            )
            selected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:00:01+00:00"
            )
            by_inst = {item["inst_id"]: item for item in selected}

            approved = by_inst[items[0]["inst_id"]]
            save_opportunity(conn, approved, review())

            transient = by_inst[items[1]["inst_id"]]
            opportunity_id = save_opportunity(conn, transient, review())
            save_execution_order(
                conn,
                {
                    "mode": "paper",
                    "route_id": "direct",
                    "status": "execution_error",
                    "notional_usd": 10.0,
                },
                transient,
                review(),
                opportunity_id=opportunity_id,
            )

            reference = by_inst[items[2]["inst_id"]]
            save_opportunity(conn, reference, review("reference_only"))

            rejected = by_inst[items[3]["inst_id"]]
            save_opportunity(conn, rejected, review("reject"))

            queue.reconcile_paper_admission_queue(
                conn, settings(), now="2026-08-07T12:00:02+00:00"
            )
            statuses = {
                row["inst_id"]: row["status"]
                for row in conn.execute("select inst_id,status from paper_admission_queue")
            }
            self.assertEqual("approved_waiting_capacity", statuses[approved["inst_id"]])
            self.assertEqual("retry_wait", statuses[transient["inst_id"]])
            self.assertEqual("terminal_reference", statuses[reference["inst_id"]])
            self.assertEqual("terminal_reject", statuses[rejected["inst_id"]])
            approved_row = conn.execute(
                "select queue_id,next_eligible_at from paper_admission_queue where inst_id=?",
                (approved["inst_id"],),
            ).fetchone()
            queue.reconcile_paper_admission_queue(
                conn, settings(), now="2026-08-07T12:01:00+00:00"
            )
            next_after = conn.execute(
                "select next_eligible_at from paper_admission_queue where queue_id=?",
                (approved_row["queue_id"],),
            ).fetchone()[0]
            self.assertEqual(approved_row["next_eligible_at"], next_after)
            reselected = queue.select_paper_admission_candidates(
                conn, settings(), now="2026-08-07T12:01:00+00:00"
            )
            self.assertIn(
                approved_row["queue_id"],
                {item["_paper_admission_queue_id"] for item in reselected},
            )

    def test_trade_and_shadow_outcomes_propagate_exact_identity(self):
        direct = candidate(
            30,
            venue="GATE",
            inst_id="BTC_USDT",
            market_surface="spot",
            trade_type="frontier_crypto_venue_map",
        )
        shadow = candidate(
            31,
            admission_key="admission-shadow",
            episode_id="episode-shadow",
            candidate_reject_reason="paper_net_edge_below_floor",
        )
        with memory_db() as conn:
            queue.enqueue_paper_admission_candidates(conn, settings(), [direct])
            queued_direct = queue.select_paper_admission_candidates(
                conn, settings(), limit=1
            )[0]
            opportunity_id = save_opportunity(conn, queued_direct, review())
            trade_id = open_paper_trade(
                conn,
                queued_direct,
                review(),
                execution={
                    "candidate": queued_direct,
                    "opportunity_id": opportunity_id,
                    "order": {
                        "mode": "paper",
                        "route_id": "gate_public_paper",
                        "status": "paper_filled",
                        "notional_usd": 100.0,
                    },
                    "fills": [
                        {"fill_price": direct["last"], "fee_bps": 0.0, "slippage_bps": 0.0}
                    ],
                },
                settings=settings(),
            )
            shadow_id = save_frontier_paper_shadow_observation(conn, shadow, review())
            old_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
            old = old_dt.isoformat()
            conn.execute("update paper_trades set opened_at=? where id=?", (old, trade_id))
            conn.execute(
                "update frontier_paper_shadow_observations set observed_at=? where id=?",
                (old, shadow_id),
            )
            conn.commit()
            observed = dt.datetime.now(dt.timezone.utc).isoformat()
            candle_open = old_dt + dt.timedelta(minutes=1)
            record_paper_price_observations(
                conn,
                [
                    {
                        "source_kind": "exchange_candle_1m_close",
                        "venue": "GATE",
                        "inst_id": "BTC_USDT",
                        "market_surface": "spot",
                        "candle_open_at": candle_open.isoformat(),
                        "event_at": (candle_open + dt.timedelta(minutes=1)).isoformat(),
                        "received_at": observed,
                        "price": 101.0,
                        "source_name": "Gate public REST spot candlesticks",
                        "source_parser": "gate_1m_candles",
                        "source_endpoint": "/api/v4/spot/candlesticks",
                        "source_event_id": "GATE|BTC_USDT|identity-test",
                        "is_closed": True,
                        "is_partial": False,
                        "freshness_state": "fresh",
                        "quality_status": "verified",
                    }
                ],
            )
            record_due_horizon_outcomes(
                conn,
                {
                    shadow["inst_id"]: {
                        "last": 102.0,
                        "observed_at": observed,
                        "price_source": "test",
                    },
                },
                settings(),
            )
            outcome = conn.execute(
                "select admission_key,admission_episode_id from paper_trade_outcomes where trade_id=?",
                (trade_id,),
            ).fetchone()
            shadow_outcome = conn.execute(
                "select admission_key,admission_episode_id from frontier_paper_shadow_outcomes where observation_id=?",
                (shadow_id,),
            ).fetchone()
            self.assertEqual(
                (queued_direct["admission_key"], queued_direct["episode_id"]),
                tuple(outcome),
            )
            self.assertEqual(("admission-shadow", "episode-shadow"), tuple(shadow_outcome))

    def test_episode_and_lineage_stats_do_not_cross_attribute(self):
        item_a = candidate(40, episode_id="episode-a")
        item_a["admission_key"] = market_admission.admission_key_for(item_a)
        item_b = candidate(
            40,
            episode_id="episode-b",
            signal_lineage_key="different-lineage",
        )
        item_b["admission_key"] = market_admission.admission_key_for(item_b)
        with memory_db() as conn:
            trade_id = open_paper_trade(conn, item_a, review())
            conn.execute(
                "update paper_trades set status='closed',pnl_bps=12.0,close_measurement_status='valid' where id=?",
                (trade_id,),
            )
            conn.execute(
                """
                insert into paper_trade_outcomes(
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,
                    measurement_status,admission_key,admission_episode_id
                ) values(?,1,?,101.0,12.0,'{}','valid',?,'episode-a')
                """,
                (
                    trade_id,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    item_a["admission_key"],
                ),
            )
            conn.commit()
            stats = market_admission._paper_stats(conn)
            exact = market_admission._stats_for(item_a, stats)
            isolated = market_admission._stats_for(item_b, stats)
            wrong_episode = market_admission._stats_for(
                {**item_a, "episode_id": "episode-new"}, stats
            )
            self.assertEqual(1, exact["valid_labels"])
            self.assertEqual("admission_episode", exact["attribution_scope"])
            self.assertEqual(0, isolated["valid_labels"])
            self.assertEqual(0, wrong_episode["valid_labels"])

    def test_required_lineage_root_isolates_canary_from_unrelated_backlog(self):
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)
        observed_at = now.isoformat()
        canary = candidate(
            48,
            strategy_lab_id=RECOVERY_CANARY_STRATEGY_LAB_ID,
            strategy_lab_lineage_root_id=RECOVERY_CANARY_STRATEGY_LAB_ID,
            source_timestamp=observed_at,
        )
        unrelated = candidate(
            49,
            score=999.0,
            strategy_lab_id="unrelated_backlog_root",
            strategy_lab_lineage_root_id="unrelated_backlog_root",
            source_timestamp=observed_at,
        )
        with memory_db() as conn:
            queued = queue.enqueue_paper_admission_candidates(
                conn, settings(), [unrelated, canary], now=observed_at
            )
            self.assertEqual(2, queued["enqueued"])

            selected = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now=(now + dt.timedelta(seconds=1)).isoformat(),
                limit=1,
                required_lineage_root=RECOVERY_CANARY_STRATEGY_LAB_ID,
            )

            self.assertEqual(1, len(selected))
            self.assertEqual(
                RECOVERY_CANARY_STRATEGY_LAB_ID,
                selected[0]["strategy_lab_lineage_root_id"],
            )
            untouched = conn.execute(
                """
                select status,selection_count,attempt_count,claim_token,claimed_by
                from paper_admission_queue where lineage_root='unrelated_backlog_root'
                """
            ).fetchone()
            self.assertEqual("queued_review", untouched["status"])
            self.assertEqual(0, untouched["selection_count"])
            self.assertEqual(0, untouched["attempt_count"])
            self.assertIsNone(untouched["claim_token"])
            self.assertIsNone(untouched["claimed_by"])

    def test_strategy_lab_canary_queue_episode_is_canonical_end_to_end(self):
        canonical_lineage = f"STRATEGY_LAB|{RECOVERY_CANARY_STRATEGY_LAB_ID}|v1"
        canary = candidate(
            50,
            strategy_lab_id=RECOVERY_CANARY_STRATEGY_LAB_ID,
            strategy_lab_version=1,
            signal_lineage_key=canonical_lineage,
            strategy_lab_lineage_root_id=RECOVERY_CANARY_STRATEGY_LAB_ID,
            signal_stats_scope="direct",
        )
        canary["admission_key"] = market_admission.admission_key_for(canary)
        canary["admission_episode_id"] = "stale-canary-episode"
        canary["paper_admission"] = {
            "admission_key": canary["admission_key"],
            "episode_id": "stale-canary-episode",
            "strategy_lineage": canonical_lineage,
            "signal_stats_scope": "direct",
            "preserved_metadata": "yes",
        }
        with memory_db() as conn:
            # Keep simulated selection times behind wall-clock time because a
            # bounded fill now rejects even slightly future quote events.
            started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3)
            queue.enqueue_paper_admission_candidates(
                conn, settings(), [canary], now=started.isoformat()
            )
            refreshed = {
                **canary,
                "last": canary["last"] + 1.0,
                "source_timestamp": (started + dt.timedelta(seconds=1)).isoformat(),
            }
            refresh_result = queue.enqueue_paper_admission_candidates(
                conn,
                settings(),
                [refreshed],
                now=(started + dt.timedelta(seconds=1)).isoformat(),
            )
            self.assertEqual(1, refresh_result["by_result"]["active_refreshed"])
            selected = queue.select_paper_admission_candidates(
                conn,
                settings(),
                now=(started + dt.timedelta(seconds=2)).isoformat(),
            )[0]
            episode = selected["episode_id"]
            self.assertNotEqual("stale-canary-episode", episode)
            self.assertEqual(episode, selected["admission_episode_id"])
            self.assertEqual(episode, selected["paper_admission"]["episode_id"])
            self.assertEqual(
                selected["admission_key"], selected["paper_admission"]["admission_key"]
            )
            self.assertEqual(
                canonical_lineage, selected["paper_admission"]["strategy_lineage"]
            )
            self.assertEqual("yes", selected["paper_admission"]["preserved_metadata"])

            trade_id = open_paper_trade(conn, selected, review(), settings=settings())
            row = dict(
                conn.execute(
                    """
                    select candidate_json,admission_key,admission_episode_id,
                           strategy_lineage_root_id
                    from paper_trades where id=?
                    """,
                    (trade_id,),
                ).fetchone()
            )
            stored_candidate = json.loads(row["candidate_json"])
            self.assertTrue(
                _recovery_canary_direct_admission_lineage_eligible(
                    row,
                    stored_candidate,
                    1,
                    {"paper_signal_stats_scope": "direct"},
                )
            )

    def test_terminal_reference_inventory_uses_terminal_queue_exit(self):
        reference = candidate(
            60,
            venue="B3",
            asset_class="reference",
            market_type="reference",
            trade_type="official_market_catalog",
            direction="watch_only",
            freshness_state="reference_static",
            route_status="unknown",
        )
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(conn, settings(), [reference])
            row = conn.execute("select status,attempt_count from paper_admission_queue").fetchone()
            state = conn.execute(
                "select health_status,terminal_class from market_admission_states"
            ).fetchone()
        self.assertEqual(1, result["terminal_audit_enqueued"])
        self.assertEqual("terminal_reference", row["status"])
        self.assertEqual(0, row["attempt_count"])
        self.assertEqual("terminal_reference", state["health_status"])
        self.assertEqual("terminal_reference", state["terminal_class"])

    def test_terminal_audit_rows_cannot_starve_active_admissions(self):
        synthetic = [
            candidate(
                100 + index,
                score=10_000.0 - index,
                route_status="paper_testable_research",
            )
            for index in range(40)
        ]
        evidence = [
            candidate(
                200 + index,
                venue="OKX",
                inst_id=f"EVIDENCE-{index}-USDT-SWAP",
                market_type="perp",
                trade_type="perp_funding_basis",
                direction="short_perp_long_spot",
                score=1.0,
            )
            for index in range(2)
        ]
        discovery = [
            candidate(300, venue="KRAKEN", score=0.5),
            candidate(301, venue="KRAKEN", score=0.4),
            candidate(302, venue="COINBASE", score=0.3),
            candidate(303, venue="COINBASE", score=0.2),
        ]
        cfg = settings(max_enqueue_per_cycle=4, max_terminal_audit_per_cycle=30)
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn, cfg, [*synthetic, *discovery, *evidence]
            )
            active = conn.execute(
                "select lane,venue from paper_admission_queue where status='queued_review'"
            ).fetchall()
            terminal_count = conn.execute(
                "select count(*) from paper_admission_queue where status='synthetic_shadow_only'"
            ).fetchone()[0]
        self.assertEqual(4, result["active_enqueued"])
        self.assertEqual(0, result["terminal_audit_enqueued"])
        self.assertEqual(0, terminal_count)
        self.assertEqual(4, result["enqueued"])
        self.assertEqual({"evidence": 2, "discovery": 2}, Counter(row["lane"] for row in active))
        self.assertEqual(
            {"KRAKEN", "COINBASE"},
            {row["venue"] for row in active if row["lane"] == "discovery"},
        )

    def test_terminal_audits_use_only_leftover_shared_enqueue_capacity(self):
        terminal = [
            candidate(
                700 + index,
                score=10_000.0 - index,
                route_status="paper_testable_research",
            )
            for index in range(10)
        ]
        active = [
            candidate(
                800 + index,
                venue="OKX",
                inst_id=f"SHARED-CAP-{index}-USDT-SWAP",
                market_type="perp",
                trade_type="perp_funding_basis",
                direction="short_perp_long_spot",
                score=1.0,
            )
            for index in range(2)
        ]
        cfg = settings(max_enqueue_per_cycle=4, max_terminal_audit_per_cycle=30)
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(
                conn,
                cfg,
                [*terminal, *active],
            )
            statuses = Counter(
                row["status"] for row in conn.execute("select status from paper_admission_queue")
            )
        self.assertEqual(4, result["enqueued"])
        self.assertEqual(2, result["active_enqueued"])
        self.assertEqual(2, result["terminal_audit_enqueued"])
        self.assertEqual(2, statuses["queued_review"])
        self.assertEqual(2, statuses["synthetic_shadow_only"])

    def test_enqueue_reserves_half_of_active_cap_for_each_lane(self):
        evidence = [
            candidate(
                400 + index,
                venue="OKX",
                inst_id=f"HALF-EVIDENCE-{index}-USDT-SWAP",
                market_type="perp",
                trade_type="perp_funding_basis",
                direction="short_perp_long_spot",
            )
            for index in range(40)
        ]
        discovery = [
            candidate(500 + index, venue="KRAKEN" if index % 2 else "COINBASE")
            for index in range(40)
        ]
        cfg = settings(max_enqueue_per_cycle=30)
        with memory_db() as conn:
            result = queue.enqueue_paper_admission_candidates(conn, cfg, [*evidence, *discovery])
            lanes = Counter(
                row["lane"]
                for row in conn.execute(
                    "select lane from paper_admission_queue where status='queued_review'"
                )
            )
        self.assertEqual(30, result["active_enqueued"])
        self.assertEqual({"evidence": 15, "discovery": 15}, lanes)


if __name__ == "__main__":
    unittest.main()
