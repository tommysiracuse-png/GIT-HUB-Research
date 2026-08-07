import copy
import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_swarm_runner
from execution_engine import build_order_ticket
from okx_perp_scanner import apply_paired_direct_entry_contract
from paired_direct_contract import calculate_paired_direct_outcome
from settings import DEFAULT_SETTINGS
from storage import init_db, open_paper_trade, save_opportunity, signal_key
from strategy_lab import (
    RECOVERY_CANARY_STRATEGY_LAB_ID,
    _allowlisted_experiment_ids,
    _evaluate_strategy_horizons,
    _experiment_outcomes,
    _rules,
    evaluate_strategy_lab,
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
    strategy_lab_summary,
    write_strategy_lab_reports,
)


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def base_settings():
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["allow_live_trading"] = False
    settings["strategy_lab"]["promote_min_active_hours"] = 48.0
    settings["strategy_lab"]["promote_min_labels"] = 30
    settings["strategy_lab"]["consecutive_passes_to_promote"] = 2
    return settings


def lab_rec():
    return {
        "recommendation_id": "rec_lab_1",
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "priority": "high",
            "title": "Test OKX spot survivor continuation",
            "rationale": "Invent a tracked sub-strategy from strong OKX spot candidates.",
            "agent_name": "strategy_lab",
            "strategy_lab_experiment": {
                "strategy_lab_id": "okx_spot_survivor_lab_v1",
                "source_surface": "frontier_spot",
                "permitted_target_surface": ["frontier_spot"],
                "hypothesis": "High-quality OKX spot frontier longs continue after dislocation.",
                "strategy_logic": {
                    "type": "candidate_filter",
                    "venues": ["OKX_SPOT"],
                    "directions": ["long_frontier_spot"],
                    "trade_types": ["frontier_crypto_venue_map"],
                    "min_edge_bps": 10,
                    "min_liquidity_score": 0.35,
                    "max_spread_bps": 8,
                },
                "data_requirements": {"required_fields": ["edge_bps_estimate"]},
                "risk_gates": {"min_edge_bps": 10},
                "promotion_rules": {"promote_min_labels": 30},
            },
        },
    }


def candidate(**overrides):
    row = {
        "venue": "OKX_SPOT",
        "inst_id": "NEAR-USDT",
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": 70.0,
        "liquidity_score": 0.75,
        "spread_bps": 2.0,
        "last": 3.5,
        "edge_bps_estimate": 18.0,
        "change_24h_pct": 1.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "execution_feasibility": {"status": "standard"},
        "target_surface": "frontier_spot",
    }
    row.update(overrides)
    return row


def mark_reliable_close(conn: sqlite3.Connection, trade_id: int, observed_at: str) -> None:
    conn.execute(
        """
        update paper_trades
        set status = 'closed', closed_at = ?, close_observed_at = ?,
            target_close_at = ?, close_delay_seconds = 0,
            close_measurement_status = 'valid'
        where id = ?
        """,
        (observed_at, observed_at, observed_at, trade_id),
    )


class StrategyLabTest(unittest.TestCase):
    def test_bounded_evaluation_requires_queue_link_to_exact_trade_artifact(self):
        strategy_id = "bounded_exact_trade_link_v1"
        admission_key = "admission:bounded-exact-trade"
        episode_id = "episode:bounded-exact-trade"
        settings = base_settings()
        settings["paper_expansion"]["enabled"] = True
        label_candidate = candidate(
            strategy_lab_id=strategy_id,
            strategy_lab_version=1,
            admission_key=admission_key,
            admission_episode_id=episode_id,
            episode_id=episode_id,
            signal_lineage_key=f"STRATEGY_LAB|{strategy_id}|v1",
        )
        review = {
            "learned_score": 75.0,
            "decision": "approve_paper_trade",
            "route_status": "standard",
            "hard_blocks": [],
        }
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with memory_db() as conn:
            # Create the trade without the bounded execution guard, then turn
            # bounded evaluation on.  The queue deliberately points at a
            # different artifact ID despite sharing the same key and episode.
            trade_id = open_paper_trade(
                conn,
                label_candidate,
                review,
                settings=base_settings(),
            )
            mark_reliable_close(conn, trade_id, now)
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id,horizon_minutes,measured_at,price,pnl_bps,
                    context_json,target_at,observed_at,delay_seconds,
                    measurement_status,price_source,admission_key,
                    admission_episode_id
                ) values (?,60,?,4.0,12.0,'{}',?,?,0,'valid','test',?,?)
                """,
                (trade_id, now, now, now, admission_key, episode_id),
            )
            conn.execute(
                """
                insert into paper_admission_queue (
                    queue_id,admission_key,episode_id,evidence_fingerprint,
                    evidence_observed_at,lane,status,priority,venue,inst_id,
                    market_surface,lineage_root,direction,route_status,
                    candidate_json,eligibility_json,enqueued_at,updated_at,
                    paper_trade_id
                ) values (?,?,?,?,?,'discovery','completed_valid',0,?,?,?,?,?,
                          'standard',?,'{}',?,?,?)
                """,
                (
                    "queue-bounded-exact-trade",
                    admission_key,
                    episode_id,
                    "fingerprint-bounded-exact-trade",
                    now,
                    label_candidate["venue"],
                    label_candidate["inst_id"],
                    label_candidate["trade_type"],
                    strategy_id,
                    label_candidate["direction"],
                    json.dumps(label_candidate, sort_keys=True),
                    now,
                    now,
                    trade_id + 1000,
                ),
            )
            conn.commit()

            outcomes = _experiment_outcomes(
                conn,
                strategy_id,
                60,
                {"strategy_lab_id": strategy_id, "version": 1},
                settings,
            )

        self.assertEqual(0, outcomes["valid_count"])
        self.assertEqual(
            1,
            outcomes["label_status_counts"]["exact_admission_lineage_mismatch"],
        )

    def test_structured_frontier_refinement_compiles_without_prose_inference(self):
        rec = {
            "recommendation_id": "rec_structured_frontier",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "title": "Isolate frontier spot longs to BITGET and GATE",
                "rationale": "Test a paper-only venue-specific frontier long refinement.",
                "market_key": "frontier_crypto_venue_map",
                "proposed_change": {
                    "source_surface": "frontier_spot",
                    "permitted_target_surface": ["frontier_spot"],
                    "asset_surface": "frontier_spot",
                    "direction": "long_only",
                    "include_venue_primary": "BITGET",
                    "include_venue_secondary": "GATE",
                },
                "variant_config": {
                    "variant_name": "frontier_spot_long_bitget_gate_only",
                    "listing_freshness_max_hours": "72",
                    "max_spread_bps": "35",
                    "min_composite_score": "0.80",
                    "min_liquidity_score": "0.70",
                },
            },
        }
        live_candidate = candidate(
            venue="BITGET",
            inst_id="ABC-USDT",
            score=90.0,
            liquidity_score=0.85,
            spread_bps=5.0,
            seen_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                """
                select experiment_type, status, strategy_logic_json
                from strategy_lab_experiments
                """
            ).fetchone()
            generated, report = generate_strategy_lab_candidates(
                conn,
                base_settings(),
                [live_candidate, {**live_candidate, "venue": "OKX_SPOT"}],
            )

        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual("market_strategy", row["experiment_type"])
        self.assertEqual("proposed", row["status"])
        self.assertEqual(["BITGET", "GATE"], logic["venues"])
        self.assertEqual(["long_frontier_spot"], logic["directions"])
        self.assertEqual(1, report["generated_candidates"])
        self.assertEqual("BITGET", generated[0]["venue"])

    def test_unstructured_risk_filter_is_not_guessed_into_market_strategy(self):
        rec = {
            "recommendation_id": "rec_unstructured_filter",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "title": "Tighten weak entries",
                "rationale": "Filter weak and stale candidates.",
            },
        }
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                "select experiment_type, status from strategy_lab_experiments"
            ).fetchone()

        self.assertEqual("risk_filter", row["experiment_type"])
        self.assertEqual("rejected_invalid", row["status"])

    def test_swarm_contains_strategy_lab_between_researcher_and_red_team(self):
        names = [agent["name"] for agent in llm_swarm_runner.AGENTS]
        self.assertIn("strategy_lab", names)
        self.assertLess(names.index("cross_market_researcher"), names.index("strategy_lab"))
        self.assertLess(names.index("strategy_lab"), names.index("red_team"))

    def test_ingests_strategy_lab_recommendation_for_runtime_compilation(self):
        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(conn, lab_rec())
            self.assertEqual("created", result[0]["action_status"])

            row = conn.execute(
                "select strategy_lab_id, experiment_type, status, hypothesis from strategy_lab_experiments"
            ).fetchone()
            self.assertEqual("okx_spot_survivor_lab_v1", row["strategy_lab_id"])
            self.assertEqual("market_strategy", row["experiment_type"])
            self.assertEqual("proposed", row["status"])
            self.assertIn("OKX spot", row["hypothesis"])

    def test_repeated_identical_recommendation_preserves_runtime_progress(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            conn.execute(
                """
                update strategy_lab_experiments
                set status='active_testing', compile_status='compiled',
                    compiled_strategy_logic_json=strategy_logic_json,
                    evaluation_json='{"valid_label_count": 7}'
                where strategy_lab_id='okx_spot_survivor_lab_v1'
                """
            )
            conn.commit()

            ingest_strategy_lab_recommendation(conn, lab_rec())
            row = conn.execute(
                """
                select status,compile_status,compiled_strategy_logic_json,evaluation_json
                from strategy_lab_experiments
                where strategy_lab_id='okx_spot_survivor_lab_v1'
                """
            ).fetchone()

        self.assertEqual("active_testing", row["status"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertTrue(json.loads(row["compiled_strategy_logic_json"]))
        self.assertEqual(7, json.loads(row["evaluation_json"])["valid_label_count"])

    def test_repeated_source_cannot_rollback_owner_repaired_contract(self):
        repaired_logic = {
            "type": "observation_program",
            "universe": {"venues": ["OKX_SPOT"]},
            "entry_expression": "return_60m_bps > 5",
            "invalidation_expression": "return_60m_bps <= 0",
            "direction": "long",
            "edge_expression": "return_60m_bps",
            "score_expression": "quality_score",
            "route_surface": "frontier_spot",
        }
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            conn.execute(
                """
                update strategy_lab_experiments
                set status='active_testing', compile_status='compiled',
                    strategy_logic_json=?, compiled_strategy_logic_json=?
                where strategy_lab_id='okx_spot_survivor_lab_v1'
                """,
                (json.dumps(repaired_logic), json.dumps(repaired_logic)),
            )
            conn.commit()

            ingest_strategy_lab_recommendation(conn, lab_rec())
            row = conn.execute(
                """
                select status,compile_status,strategy_logic_json
                from strategy_lab_experiments
                where strategy_lab_id='okx_spot_survivor_lab_v1'
                """
            ).fetchone()

        self.assertEqual("active_testing", row["status"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual(repaired_logic, json.loads(row["strategy_logic_json"]))

    def test_repairable_compiled_strategy_remains_in_runtime_portfolio(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, base_settings(), [candidate()])
            conn.execute(
                """
                update strategy_lab_experiments set status='needs_contract_revision'
                where strategy_lab_id='okx_spot_survivor_lab_v1'
                """
            )
            conn.commit()

            generated, report = generate_strategy_lab_candidates(
                conn, base_settings(), [candidate()]
            )

        self.assertEqual(1, len(generated))
        self.assertEqual(1, report["active_experiments"])

    def test_ingests_explicit_experiment_type(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "route_filter_lab_v1"
        experiment["experiment_type"] = "execution_filter"
        experiment["hypothesis"] = "Require route quality gates before frontier long entries."

        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(conn, rec)
            row = conn.execute(
                "select experiment_type from strategy_lab_experiments where strategy_lab_id = ?",
                ("route_filter_lab_v1",),
            ).fetchone()

        self.assertEqual("execution_filter", result[0]["experiment_type"])
        self.assertEqual("execution_filter", row["experiment_type"])

    def test_infers_non_market_experiment_types(self):
        repair_rec = {
            "recommendation_id": "rec_repair",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "title": "Repair malformed JSON recommendation output",
                "rationale": "Schema parser failures are creating fake strategy tasks.",
            },
        }
        risk_rec = {
            "recommendation_id": "rec_risk",
            "payload": {
                "action": "propose_strategy_lab_experiment",
                "title": "Cooldown weak Yahoo proxy short false positives",
                "rationale": "Reduce failing entries after repeated decay and weak win rate.",
            },
        }

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, repair_rec)
            ingest_strategy_lab_recommendation(conn, risk_rec)
            rows = conn.execute(
                "select strategy_lab_id, experiment_type from strategy_lab_experiments"
            ).fetchall()
            by_id = {row["strategy_lab_id"]: row["experiment_type"] for row in rows}

        self.assertIn("system_repair", set(by_id.values()))
        self.assertIn("risk_filter", set(by_id.values()))

    def test_candidate_generation_emits_standard_candidate_with_lab_id(self):
        settings = base_settings()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, report = generate_strategy_lab_candidates(conn, settings, [candidate()])

            self.assertEqual(1, len(generated))
            self.assertEqual(1, report["generated_candidates"])
            self.assertEqual("okx_spot_survivor_lab_v1", generated[0]["strategy_lab_id"])
            self.assertEqual("market_strategy", generated[0]["strategy_lab_experiment_type"])
            self.assertEqual("frontier_crypto_venue_map", generated[0]["trade_type"])
            self.assertEqual("long_frontier_spot", generated[0]["direction"])
            self.assertGreater(generated[0]["score"], 70.0)
            self.assertTrue(signal_key(generated[0]).startswith("STRATEGY_LAB|okx_spot_survivor_lab_v1|"))

    def test_candidate_generation_resolves_equivalent_runtime_field_names(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "field_alias_lab_v1"
        experiment["strategy_logic"]["required_fields"] = ["edge_bps", "stale_minutes", "detected_at"]
        experiment["strategy_logic"]["max_stale_minutes"] = 1.0
        source = candidate(
            edge_bps_estimate=18.0,
            freshness_age_seconds=30.0,
            seen_at="2026-07-30T00:00:00+00:00",
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [source])

        self.assertEqual(1, len(generated), report)
        self.assertEqual("field_alias_lab_v1", generated[0]["strategy_lab_id"])

    def test_candidate_generation_normalizes_feature_scales_and_broad_dimensions(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "normalized_okx_funding_lab_v1"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "venues": ["OKX"],
            "trade_types": ["perp_funding_basis"],
            "directions": ["funding_capture_long_perp"],
            "regions": ["global"],
            "asset_classes": ["crypto"],
            "required_fields": ["quality_score", "stale_minutes", "timestamp"],
            "min_score": 0.6,
            "min_liquidity_score": 55,
            "min_quality_score": 0.65,
            "max_spread_bps": 8,
        }
        experiment["source_surface"] = "perp_funding_basis"
        experiment["permitted_target_surface"] = ["perp_funding_basis"]
        source = candidate(
            venue="OKX",
            inst_id="BTC-USDT-SWAP",
            trade_type="perp_funding_basis",
            direction="funding_capture_long_perp",
            asset_class="crypto_linked_derivative",
            score=72.0,
            liquidity_score=0.8,
            spread_bps=2.0,
            seen_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            hedge_venue="OKX_SPOT",
            hedge_instrument="BTC-USDT",
            fee_model="paper_conservative_v1",
            paper_leg_mapping_valid=True,
            target_surface="perp_funding_basis",
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [source])

        self.assertEqual(1, len(generated), report)
        features = generated[0]["strategy_lab_normalized_features"]
        self.assertEqual("crypto", features["asset_class"])
        self.assertEqual("global", features["region"])
        self.assertGreaterEqual(features["quality_score"], 65.0)

    def test_compiled_contract_survives_closed_session_using_persisted_runtime_evidence(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "jse_dormant_session_lab_v1"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "venues": ["JOHANNESBURG_STOCK_EXCHANGE"],
            "trade_types": ["global_market_discovery_proxy"],
            "directions": ["long_proxy"],
            "required_fields": ["edge_bps", "timestamp"],
            "min_edge_bps": 10,
        }
        experiment["source_surface"] = "proxy"
        experiment["permitted_target_surface"] = ["proxy"]
        historical = candidate(
            venue="JOHANNESBURG_STOCK_EXCHANGE",
            inst_id="JOHANNESBURG_STOCK_EXCHANGE:SBSW",
            trade_type="global_market_discovery_proxy",
            direction="long_proxy",
            seen_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            target_surface="proxy",
        )
        review = {
            "learned_score": 70.0,
            "decision": "approve_paper_trade",
            "route_status": "standard",
            "hard_blocks": [],
        }

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            save_opportunity(conn, historical, review)
            generated, report = generate_strategy_lab_candidates(
                conn,
                base_settings(),
                [candidate(venue="OTHER_VENUE")],
            )
            row = conn.execute(
                "select status, compile_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("jse_dormant_session_lab_v1",),
            ).fetchone()
            recovered, _ = generate_strategy_lab_candidates(conn, base_settings(), [historical])

        self.assertEqual([], generated)
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("needs_more_evidence", row["status"])
        self.assertEqual({"compiled": 1}, report["contract_compilation"]["by_compile_status"])
        self.assertEqual(1, len(recovered))

    def test_scope_metadata_is_not_treated_as_missing_candidate_data(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "scope_metadata_repair_lab_v1"
        experiment["strategy_logic"]["required_fields"] = [
            "venues",
            "trade_types",
            "directions",
            "edge_bps",
        ]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])
            row = conn.execute(
                "select strategy_logic_json, compile_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("scope_metadata_repair_lab_v1",),
            ).fetchone()

        logic = json.loads(row["strategy_logic_json"])
        self.assertEqual(["edge_bps"], logic["required_fields"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual(1, len(generated), report)

    def test_candidate_generation_does_not_treat_watch_only_as_paper_testable(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, report = generate_strategy_lab_candidates(
                conn,
                base_settings(),
                [candidate(direction="watch_only", score=100.0)],
            )

        self.assertEqual([], generated)
        reasons = report["reject_reasons_by_experiment"]["okx_spot_survivor_lab_v1"]
        self.assertIn("watch_only_not_paper_testable", reasons)

    def test_unscoped_strategy_contract_cannot_generate_paper_candidates(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "unscoped_lab_v1"
        experiment["strategy_logic"] = {"type": "candidate_filter"}

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            stored = conn.execute(
                "select status from strategy_lab_experiments where strategy_lab_id = ?",
                ("unscoped_lab_v1",),
            ).fetchone()
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])

        self.assertEqual("needs_data", stored["status"])
        self.assertEqual([], generated)
        reasons = report["reject_reasons_by_experiment"]["unscoped_lab_v1"]
        self.assertIn("missing_strategy_scope", reasons)

    def test_explicit_cross_surface_contract_can_generate_candidates(self):
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "cross_surface_lab_v1"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "allow_any_surface": True,
            "min_edge_bps": 10,
            "min_liquidity_score": 0.35,
        }
        experiment["source_surface"] = "proxy_momentum"
        experiment["permitted_target_surface"] = ["frontier_spot"]

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            generated, _report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])

        self.assertEqual(1, len(generated))

    def test_candidate_generation_prefers_standard_paper_route(self):
        settings = base_settings()
        settings["strategy_lab"]["max_candidates_per_experiment"] = 1
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, _report = generate_strategy_lab_candidates(
                conn,
                settings,
                [
                    candidate(inst_id="CONDITIONAL", score=100.0, route_status="conditional"),
                    candidate(inst_id="STANDARD", score=70.0, route_status="standard"),
                ],
            )

        self.assertEqual("STANDARD", generated[0]["inst_id"])

    def test_zero_output_is_truthfully_diagnosed_and_can_recover(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, report = generate_strategy_lab_candidates(
                conn,
                base_settings(),
                [candidate(venue="OTHER_VENUE")],
            )
            self.assertEqual([], generated)
            self.assertEqual("needs_data", report["status_by_experiment"]["okx_spot_survivor_lab_v1"])
            row = conn.execute(
                "select status, evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()
            self.assertEqual("needs_data", row["status"])
            diagnostic = json.loads(row["evaluation_json"])["contract_compilation"]
            self.assertTrue(diagnostic["nearest_candidates"])

            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])
            self.assertEqual(1, len(generated))
            self.assertEqual("active_testing", report["status_by_experiment"]["okx_spot_survivor_lab_v1"])

    def test_misplaced_direction_in_trade_types_is_repaired(self):
        bad_rec = lab_rec()
        experiment = bad_rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "frontier_bad_field_repaired"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "venues": ["okx_spot"],
            "trade_types": ["long_frontier_spot"],
            "directions": ["long"],
            "min_edge_bps": 10,
            "min_liquidity_score": 0.35,
            "max_spread_bps": 8,
        }

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, bad_rec)
            row = conn.execute(
                "select strategy_logic_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("frontier_bad_field_repaired",),
            ).fetchone()
            logic = json.loads(row["strategy_logic_json"])

            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])

        self.assertEqual(["frontier_crypto_venue_map"], logic["trade_types"])
        self.assertIn("long_frontier_spot", logic["directions"])
        self.assertIn("moved_trade_type_direction:long_frontier_spot", logic["normalization_notes"])
        self.assertEqual(1, len(generated))
        self.assertEqual(1, report["generated_candidates"])

    def test_generic_direction_can_match_specific_direction(self):
        generic_rec = lab_rec()
        experiment = generic_rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "generic_long_direction_lab"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "venues": ["OKX_SPOT"],
            "trade_types": ["frontier_crypto_venue_map"],
            "directions": ["long"],
            "min_edge_bps": 10,
            "min_liquidity_score": 0.35,
            "max_spread_bps": 8,
        }

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, generic_rec)
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [candidate()])

        self.assertEqual(1, len(generated))
        self.assertEqual(1, report["generated_candidates"])

    def test_runtime_vocabulary_allows_new_strategy_surfaces(self):
        dynamic_rec = lab_rec()
        experiment = dynamic_rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "dynamic_surface_lab"
        experiment["strategy_logic"] = {
            "type": "candidate_filter",
            "venues": ["NEW_VENUE"],
            "trade_types": ["buy_local_sell_reference"],
            "min_edge_bps": 10,
            "min_liquidity_score": 0.35,
            "max_spread_bps": 8,
        }
        experiment["source_surface"] = "regional_cross_reference_spread"
        experiment["permitted_target_surface"] = ["regional_cross_reference_spread"]
        new_candidate = candidate(
            venue="NEW_VENUE",
            inst_id="NEW_VENUE:ABC",
            trade_type="regional_cross_reference_spread",
            direction="buy_local_sell_reference",
            target_surface="regional_cross_reference_spread",
        )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, dynamic_rec)
            generated, report = generate_strategy_lab_candidates(conn, base_settings(), [new_candidate])

        self.assertEqual(1, len(generated))
        self.assertEqual("regional_cross_reference_spread", generated[0]["trade_type"])
        self.assertEqual("buy_local_sell_reference", generated[0]["direction"])
        self.assertEqual(1, report["generated_candidates"])

    def test_strategy_lab_prompt_explains_trade_type_direction_split(self):
        prompt = llm_swarm_runner.agent_prompt(
            next(agent for agent in llm_swarm_runner.AGENTS if agent["name"] == "strategy_lab"),
            {"allowed_recommendation_actions": ["propose_strategy_lab_experiment"]},
            [],
        )
        self.assertIn("trade_types are scanner families", prompt)
        self.assertIn("Do not put a direction in trade_types", prompt)
        self.assertIn("experiment_type must be one of", prompt)
        self.assertIn("Paper exploration is enabled", prompt)
        self.assertIn("do not propose new hard quarantines", prompt)

    def test_lab_id_persists_through_opportunity_and_paper_trade(self):
        settings = base_settings()
        with memory_db() as conn:
            lab_candidate = candidate(strategy_lab_id="lab_persist_v1", strategy_lab_version=1)
            review = {
                "learned_score": 71.0,
                "decision": "approve_paper_trade",
                "route_status": "standard",
                "hard_blocks": [],
            }
            save_opportunity(conn, lab_candidate, review)
            trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)

            opportunity = conn.execute("select strategy_lab_id from opportunities").fetchone()
            trade = conn.execute("select strategy_lab_id, signal_key from paper_trades where id = ?", (trade_id,)).fetchone()
            self.assertEqual("lab_persist_v1", opportunity["strategy_lab_id"])
            self.assertEqual("lab_persist_v1", trade["strategy_lab_id"])
            self.assertTrue(trade["signal_key"].startswith("STRATEGY_LAB|lab_persist_v1|"))

    def test_evaluator_queues_code_change_after_second_promotion_pass(self):
        settings = base_settings()
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])
            conn.execute(
                """
                update strategy_lab_experiments
                set created_at = ?, consecutive_passes = 1
                where strategy_lab_id = ?
                """,
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = candidate(
                    inst_id=f"NEAR-{idx}",
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, ?, ?, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, 4.0, 14.0, created_at, created_at),
                )
            conn.commit()

            report = evaluate_strategy_lab(conn, settings)
            row = conn.execute(
                "select status, promoted_proposal_id from strategy_lab_experiments where strategy_lab_id = ?",
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()
            rec = conn.execute("select action, payload_json from llm_recommendations").fetchone()

            self.assertEqual("promotion_queued", row["status"])
            self.assertIsNotNone(row["promoted_proposal_id"])
            self.assertEqual("propose_code_change", rec["action"])
            payload = json.loads(rec["payload_json"])
            self.assertEqual("strategy_lab_promotion", payload["change_category"])
            self.assertEqual("promotion_queued", report["evaluated"][0]["decision"])

    def test_evaluator_promotes_at_profitable_strategy_specific_horizon(self):
        settings = base_settings()
        settings["strategy_lab"]["candidate_horizons_minutes"] = [60, 240]
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])
            conn.execute(
                "update strategy_lab_experiments set created_at = ?, status = 'retired_bad_evidence' where strategy_lab_id = ?",
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = candidate(
                    inst_id=f"HORIZON-{idx}",
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                for horizon, pnl in ((60, -20.0), (240, 14.0)):
                    conn.execute(
                        """
                        insert into paper_trade_outcomes (
                            trade_id, horizon_minutes, measured_at, price, pnl_bps,
                            context_json, target_at, observed_at, delay_seconds,
                            measurement_status, price_source
                        ) values (?, ?, ?, 4.0, ?, '{}', ?, ?, 0, 'valid', 'test')
                        """,
                        (trade_id, horizon, created_at, pnl, created_at, created_at),
                    )
            conn.commit()

            first = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            second = evaluate_strategy_lab(conn, settings)["evaluated"][0]

        self.assertEqual(240, first["selected_horizon_minutes"])
        self.assertEqual("promotion_gate_passed", first["decision"])
        self.assertEqual("active_testing", first["status"])
        self.assertEqual("promotion_queued", second["decision"])

    def test_evaluator_backfills_route_cost_before_promotion(self):
        settings = base_settings()
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        source = candidate(
            gross_edge_bps_estimate=60.0,
            estimated_round_trip_cost_bps=12.0,
            freshness_age_seconds=30.0,
            paper_context_cost_gate={
                "paper_only": True,
                "applicable": True,
                "enabled": True,
                "eligible": True,
                "context_cost_floor_bps": 25.0,
                "inputs": {"route_status": "conditional"},
                "quality_floor": {"applies": True, "passed": True},
            },
        )
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, _ = generate_strategy_lab_candidates(conn, settings, [source])
            self.assertEqual(1, len(generated))
            conn.execute(
                "update strategy_lab_experiments set created_at = ?, consecutive_passes = 1 where strategy_lab_id = ?",
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = {
                    **generated[0],
                    "inst_id": f"ROUTE-COST-{idx}",
                }
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "conditional",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 4.0, 14.0, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, created_at, created_at),
                )
            conn.commit()

            evaluation = evaluate_strategy_lab(conn, settings)["evaluated"][0]

        self.assertNotEqual("promotion_queued", evaluation["decision"])
        self.assertLess(evaluation["metrics"]["avg_pnl_bps"], 10.0)
        self.assertEqual(
            30,
            evaluation["realized_cost_backfill"]["applied_count"],
        )

    def test_evaluator_requires_balanced_direction_evidence_when_configured(self):
        settings = base_settings()
        rec = lab_rec()
        experiment = rec["payload"]["strategy_lab_experiment"]
        experiment["strategy_lab_id"] = "balanced_direction_lab"
        experiment["strategy_logic"]["directions"] = ["long_frontier_spot", "short_frontier_spot"]
        experiment["promotion_rules"] = {
            "promote_min_labels": 30,
            "promote_min_active_hours": 48,
            "promote_min_avg_pnl_bps": 10,
            "promote_min_win_rate": 0.53,
            "promote_min_valid_label_rate": 0.9,
            "promote_worst_decile_floor_bps": -45,
            "min_labels_per_direction": 20,
            "min_avg_pnl_bps_per_direction": 0,
            "min_win_rate_per_direction": 0.48,
            "consecutive_passes_to_promote": 2,
        }
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()

        def add_outcomes(conn, direction, start, count):
            for idx in range(start, start + count):
                lab_candidate = candidate(
                    inst_id=f"{direction}-{idx}",
                    direction=direction,
                    strategy_lab_id="balanced_direction_lab",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 4, 14, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, created_at, created_at),
                )

        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, rec)
            generate_strategy_lab_candidates(
                conn,
                settings,
                [candidate(), candidate(inst_id="SHORT", direction="short_frontier_spot")],
            )
            conn.execute(
                """
                update strategy_lab_experiments
                set created_at = ?, consecutive_passes = 1
                where strategy_lab_id = 'balanced_direction_lab'
                """,
                (created_at,),
            )
            add_outcomes(conn, "long_frontier_spot", 0, 30)
            add_outcomes(conn, "short_frontier_spot", 0, 10)
            conn.commit()

            first = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            self.assertFalse(first["direction_promotion"]["passed"])
            self.assertEqual(
                ["min_labels"],
                first["direction_promotion"]["checks"]["short_frontier_spot"]["failed_thresholds"],
            )
            self.assertIsNone(first["promotion_recommendation_id"])

            add_outcomes(conn, "short_frontier_spot", 10, 10)
            conn.execute(
                "update strategy_lab_experiments set status = 'active_testing', consecutive_passes = 1 where strategy_lab_id = 'balanced_direction_lab'"
            )
            conn.commit()
            second = evaluate_strategy_lab(conn, settings)["evaluated"][0]

        self.assertTrue(second["direction_promotion"]["passed"])
        self.assertEqual("promotion_queued", second["decision"])

    def test_evaluator_uses_only_reliable_exact_version_attribution(self):
        settings = base_settings()
        settings["strategy_lab"]["candidate_horizons_minutes"] = [60]
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])

            def add_trade(
                inst_id: str,
                *,
                version: int = 1,
                reliable: bool = True,
                blocked: bool = False,
                mismatched_candidate_version: bool = False,
            ) -> int:
                lab_candidate = candidate(
                    inst_id=inst_id,
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=version,
                )
                if blocked:
                    lab_candidate["route_status"] = "conditional"
                    lab_candidate["route_blockers"] = ["unresolved_permission"]
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "conditional" if blocked else "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                if reliable:
                    mark_reliable_close(conn, trade_id, dt.datetime.now(dt.timezone.utc).isoformat())
                if mismatched_candidate_version:
                    stored = conn.execute(
                        "select candidate_json from paper_trades where id = ?", (trade_id,)
                    ).fetchone()
                    payload = json.loads(stored["candidate_json"])
                    payload["strategy_lab_version"] = 2
                    conn.execute(
                        "update paper_trades set candidate_json = ? where id = ?",
                        (json.dumps(payload, sort_keys=True), trade_id),
                    )
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 4.0, 20.0, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, now, now, now),
                )
                return trade_id

            add_trade("GOOD")
            add_trade("NO-CLOSE", reliable=False)
            add_trade("BLOCKED", blocked=True)
            add_trade("WRONG-CANDIDATE-VERSION", mismatched_candidate_version=True)
            add_trade("OTHER-VERSION", version=2)
            conn.commit()

            result = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            stored_evaluation = json.loads(
                conn.execute(
                    "select evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
                    ("okx_spot_survivor_lab_v1",),
                ).fetchone()["evaluation_json"]
            )

        outcomes = stored_evaluation["outcomes"]
        self.assertEqual(1, result["metrics"]["count"])
        self.assertEqual(4, outcomes["trade_count"])
        self.assertEqual(0.25, outcomes["valid_label_rate"])
        self.assertEqual(1, outcomes["label_status_counts"]["strategy_lab_attribution_mismatch"])
        self.assertTrue(
            any(
                key.startswith("paper_label_ineligible:")
                for key in outcomes["label_status_counts"]
            )
        )

    def test_root_allowlist_includes_descendants_and_excludes_other_roots(self):
        settings = base_settings()
        settings["strategy_lab"]["experiment_root_allowlist"] = ["allowed_root"]
        settings["strategy_lab"]["max_candidates_per_experiment"] = 1
        settings["strategy_lab"]["max_candidates_per_loop"] = 5
        records = []
        for strategy_lab_id, parent_id in (
            ("allowed_root", None),
            ("allowed_child", "allowed_root"),
            ("blocked_root", None),
        ):
            rec = copy.deepcopy(lab_rec())
            rec["recommendation_id"] = f"rec_{strategy_lab_id}"
            experiment = rec["payload"]["strategy_lab_experiment"]
            experiment["strategy_lab_id"] = strategy_lab_id
            if parent_id:
                experiment["parent_strategy_lab_id"] = parent_id
            records.append(rec)

        with memory_db() as conn:
            for rec in records:
                ingest_strategy_lab_recommendation(conn, rec)
            generated, report = generate_strategy_lab_candidates(
                conn,
                settings,
                [candidate()],
            )

        self.assertEqual(
            {"allowed_root", "allowed_child"},
            {row["strategy_lab_id"] for row in generated},
        )
        self.assertTrue(
            all(row["strategy_lab_lineage_root_id"] == "allowed_root" for row in generated)
        )
        self.assertEqual(["allowed_root"], report["experiment_root_allowlist"])

    def test_recovery_root_allowlist_does_not_revive_historical_children(self):
        settings = base_settings()
        settings["operations"]["fail_closed_recovery_profile"] = True
        settings["strategy_lab"].update(
            {
                "experiment_root_allowlist": ["allowed_root"],
                "max_candidates_per_experiment": 1,
                "max_candidates_per_loop": 5,
                "adaptive_relaxation_enabled": False,
                "region_splits_enabled": False,
            }
        )
        records = []
        for strategy_lab_id, parent_id in (
            ("allowed_root", None),
            ("historical_relaxed_child", "allowed_root"),
            ("historical_region_child", "allowed_root"),
        ):
            rec = copy.deepcopy(lab_rec())
            rec["recommendation_id"] = f"rec_{strategy_lab_id}"
            experiment = rec["payload"]["strategy_lab_experiment"]
            experiment["strategy_lab_id"] = strategy_lab_id
            if parent_id:
                experiment["parent_strategy_lab_id"] = parent_id
            records.append(rec)

        with memory_db() as conn:
            for rec in records:
                ingest_strategy_lab_recommendation(conn, rec)
            generated, report = generate_strategy_lab_candidates(
                conn,
                settings,
                [candidate()],
            )

        self.assertEqual(
            ["allowed_root"],
            [row["strategy_lab_id"] for row in generated],
        )
        self.assertTrue(report["controls"]["experiment_root_only"])
        self.assertEqual(1, report["allowlisted_experiment_count"])

    def test_active_root_cap_limits_runtime_to_six_roots(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "experiment_root_allowlist": [],
                "max_active_strategy_roots": 6,
                "max_candidates_per_experiment": 1,
                "max_candidates_per_loop": 10,
            }
        )
        with memory_db() as conn:
            for index in range(7):
                rec = copy.deepcopy(lab_rec())
                rec["recommendation_id"] = f"root_cap_rec_{index}"
                rec["payload"]["strategy_lab_experiment"]["strategy_lab_id"] = (
                    f"root_cap_{index}"
                )
                ingest_strategy_lab_recommendation(conn, rec)
            conn.execute(
                "update strategy_lab_experiments set status='active_testing',compile_status='compiled'"
            )
            conn.commit()
            allowed, roots = _allowlisted_experiment_ids(
                conn,
                [],
                6,
            )

        self.assertEqual(6, len({roots[experiment_id] for experiment_id in allowed or set()}))

    def test_promotion_requires_untouched_chronological_holdout(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "candidate_horizons_minutes": [60],
                "promote_min_labels": 100,
                "promote_min_training_labels": 100,
                "promote_min_holdout_labels": 50,
                "promote_min_active_hours": 168,
                "promote_min_avg_pnl_bps": 10.0,
                "promote_min_win_rate": 0.53,
                "promote_worst_decile_floor_bps": -45.0,
                "promote_min_valid_label_rate": 0.9,
                "consecutive_passes_to_promote": 2,
                "promotion_enabled": False,
                "recommendation_emission_enabled": False,
                "lifecycle_mutations_enabled": True,
                "region_splits_enabled": False,
            }
        )
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])
            conn.execute(
                "update strategy_lab_experiments set created_at=? where strategy_lab_id=?",
                (created_at, "okx_spot_survivor_lab_v1"),
            )

            def add_labels(start: int, count: int) -> None:
                for index in range(start, start + count):
                    label_candidate = candidate(
                        inst_id=f"HOLDOUT-{index}",
                        strategy_lab_id="okx_spot_survivor_lab_v1",
                        strategy_lab_version=1,
                    )
                    review = {
                        "learned_score": 75.0,
                        "decision": "approve_paper_trade",
                        "route_status": "standard",
                        "hard_blocks": [],
                    }
                    trade_id = open_paper_trade(
                        conn,
                        label_candidate,
                        review,
                        settings=settings,
                    )
                    measured_at = (
                        dt.datetime.fromisoformat(created_at)
                        + dt.timedelta(minutes=index + 1)
                    ).isoformat()
                    mark_reliable_close(conn, trade_id, measured_at)
                    conn.execute(
                        """
                        insert into paper_trade_outcomes(
                            trade_id,horizon_minutes,measured_at,price,pnl_bps,
                            context_json,target_at,observed_at,delay_seconds,
                            measurement_status,price_source
                        ) values(?,60,?,4.0,12.0,'{}',?,?,0,'valid','test')
                        """,
                        (trade_id, measured_at, measured_at, measured_at),
                    )
                conn.commit()

            add_labels(0, 149)
            before_holdout = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            add_labels(149, 1)
            first_pass = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            second_pass = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            stored = conn.execute(
                "select status,consecutive_passes,evaluation_json from strategy_lab_experiments where strategy_lab_id=?",
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()

        self.assertNotEqual("promotion_gate_passed", before_holdout["decision"])
        self.assertEqual("promotion_gate_passed", first_pass["decision"])
        self.assertEqual("promotion_candidate", second_pass["decision"])
        self.assertEqual("promotion_candidate", stored["status"])
        self.assertEqual(2, stored["consecutive_passes"])
        split = json.loads(stored["evaluation_json"])["outcomes"]["promotion_split"]
        self.assertEqual(100, split["training"]["count"])
        self.assertEqual(50, split["holdout"]["count"])

    def test_horizon_selection_uses_training_and_keeps_holdout_untouched(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "candidate_horizons_minutes": [60, 240],
                "promote_min_labels": 150,
                "promote_min_training_labels": 100,
                "promote_min_holdout_labels": 50,
                "promote_min_active_hours": 168,
                "promote_min_avg_pnl_bps": 10.0,
                "promote_min_win_rate": 0.53,
                "promote_worst_decile_floor_bps": -45.0,
                "promote_min_valid_label_rate": 0.9,
            }
        )
        experiment = {
            "strategy_lab_id": "training_selected_horizon",
            "strategy_logic": {},
            "promotion_rules": {},
            "evaluation": {},
        }
        rules = _rules(settings, experiment)

        def stats(count, avg, win_rate, worst):
            return {
                "count": count,
                "avg_pnl_bps": avg,
                "raw_avg_pnl_bps": avg,
                "trimmed_mean_bps": avg,
                "win_rate": win_rate,
                "raw_win_rate": win_rate,
                "worst_decile_pnl_bps": worst,
                "raw_worst_decile_pnl_bps": worst,
            }

        def horizon_outcomes(_conn, _strategy_id, horizon, *_args):
            if int(horizon) == 60:
                training = stats(100, 30.0, 0.65, -20.0)
                holdout = stats(50, -20.0, 0.40, -60.0)
            else:
                training = stats(100, 15.0, 0.56, -30.0)
                holdout = stats(50, 18.0, 0.60, -25.0)
            return {
                "metrics": stats(150, 14.0, 0.56, -35.0),
                "promotion_split": {
                    "chronological": True,
                    "training": training,
                    "holdout": holdout,
                },
                "route_status_counts": {"standard": 150},
                "valid_label_rate": 1.0,
                "valid_label_rate_raw": 1.0,
                "by_direction": {},
            }

        with memory_db() as conn, mock.patch(
            "strategy_lab._experiment_outcomes", side_effect=horizon_outcomes
        ):
            evaluation = _evaluate_strategy_horizons(
                conn,
                experiment,
                rules,
                settings,
                active_hours=168,
            )

        self.assertEqual(60, evaluation["selected_horizon_minutes"])
        self.assertEqual(
            "chronological_training",
            evaluation["selected"]["horizon_selection_partition"],
        )
        self.assertEqual(30.0, evaluation["selected"]["selection_score_bps"])
        self.assertFalse(evaluation["selected"]["promote_ready"])
        self.assertTrue(evaluation["horizon_evaluations"]["240"]["promote_ready"])
        self.assertEqual(
            -20.0,
            evaluation["selected"]["promotion_gate_metrics"]["raw_avg_pnl_bps"],
        )

    def test_promotion_thresholds_compare_unrounded_evidence(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "candidate_horizons_minutes": [60],
                "promote_min_labels": 100,
                "promote_min_training_labels": 100,
                "promote_min_holdout_labels": 50,
                "promote_min_active_hours": 168,
                "promote_min_avg_pnl_bps": 10.0,
                "promote_min_win_rate": 0.53,
                "promote_worst_decile_floor_bps": -45.0,
                "promote_min_valid_label_rate": 0.9,
            }
        )
        experiment = {
            "strategy_lab_id": "raw_threshold_lab",
            "strategy_logic": {},
            "promotion_rules": {},
            "evaluation": {},
        }
        rules = _rules(settings, experiment)

        def passing_metrics(count: int) -> dict:
            return {
                "count": count,
                "avg_pnl_bps": 10.0,
                "raw_avg_pnl_bps": 10.0,
                "median_pnl_bps": 10.0,
                "trimmed_mean_bps": 10.0,
                "win_rate": 0.53,
                "raw_win_rate": 0.53,
                "worst_decile_pnl_bps": -45.0,
                "raw_worst_decile_pnl_bps": -45.0,
                "min_pnl_bps": -45.0,
                "max_pnl_bps": 20.0,
            }

        base_outcomes = {
            "metrics": passing_metrics(150),
            "promotion_split": {
                "training": passing_metrics(100),
                "holdout": passing_metrics(50),
            },
            "route_status_counts": {"standard": 150},
            "valid_label_rate": 0.9,
            "valid_label_rate_raw": 0.9,
            "by_direction": {},
        }
        cases = {
            "average": ("raw_avg_pnl_bps", 9.9996, None),
            "win_rate": ("raw_win_rate", 0.5296, None),
            "worst_decile": ("raw_worst_decile_pnl_bps", -45.0004, None),
            "valid_rate": (None, None, 0.8996),
        }

        with memory_db() as conn:
            for name, (metric_key, metric_value, valid_rate) in cases.items():
                outcomes = copy.deepcopy(base_outcomes)
                if metric_key is not None:
                    outcomes["promotion_split"]["holdout"][metric_key] = metric_value
                if valid_rate is not None:
                    outcomes["valid_label_rate_raw"] = valid_rate
                with self.subTest(name=name), mock.patch(
                    "strategy_lab._experiment_outcomes", return_value=outcomes
                ):
                    evaluation = _evaluate_strategy_horizons(
                        conn,
                        experiment,
                        rules,
                        settings,
                        active_hours=168,
                    )
                    self.assertFalse(evaluation["selected"]["promote_ready"])

    def test_snapshot_warmup_can_run_with_generation_and_mutations_disabled(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "snapshot_warmup_enabled": True,
                "runtime_generation_enabled": False,
                "evaluation_enabled": False,
                "lifecycle_mutations_enabled": False,
                "recommendation_emission_enabled": False,
                "promotion_enabled": False,
                "adaptive_relaxation_enabled": False,
            }
        )
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        with memory_db() as conn:
            generated, report = generate_strategy_lab_candidates(
                conn,
                settings,
                [],
                [
                    {
                        **candidate(),
                        "observed_at": observed_at,
                        "price_source": "test",
                    }
                ],
            )
            stored = conn.execute("select count(*) from strategy_feature_snapshots").fetchone()[0]
            evaluation = evaluate_strategy_lab(conn, settings)

        self.assertEqual([], generated)
        self.assertEqual(1, stored)
        self.assertFalse(report["controls"]["runtime_generation_enabled"])
        self.assertFalse(report["controls"]["lifecycle_mutations_enabled"])
        self.assertEqual("strategy_lab_evaluation_disabled", evaluation["reason"])

    def test_recovery_snapshot_warmup_rejects_missing_source_timestamp(self):
        settings = base_settings()
        settings["operations"]["fail_closed_recovery_profile"] = True
        settings["market_admission"].setdefault("paper_queue", {})[
            "max_freshness_age_seconds"
        ] = 90.0
        settings["strategy_lab"].update(
            {
                "experiment_root_allowlist": [],
                "snapshot_warmup_enabled": True,
                "runtime_generation_enabled": False,
                "evaluation_enabled": False,
                "lifecycle_mutations_enabled": False,
            }
        )
        row = candidate(inst_id="MISSING-TIME-USDT")
        for field in (
            "exchange_timestamp",
            "source_timestamp",
            "source_observed_at",
            "observed_at",
            "seen_at",
            "timestamp",
        ):
            row.pop(field, None)

        with memory_db() as conn:
            generated, report = generate_strategy_lab_candidates(
                conn, settings, [], [row]
            )
            stored = conn.execute(
                "select count(*) from strategy_feature_snapshots"
            ).fetchone()[0]

        self.assertEqual([], generated)
        self.assertEqual(0, stored)
        self.assertEqual(0, report["feature_snapshots"]["feature_frames"])

    def test_recovery_snapshot_warmup_rejects_stale_but_keeps_fresh_event_time(self):
        settings = base_settings()
        settings["operations"]["fail_closed_recovery_profile"] = True
        settings["market_admission"].setdefault("paper_queue", {})[
            "max_freshness_age_seconds"
        ] = 90.0
        settings["strategy_lab"].update(
            {
                "experiment_root_allowlist": [],
                "snapshot_warmup_enabled": True,
                "runtime_generation_enabled": False,
                "evaluation_enabled": False,
                "lifecycle_mutations_enabled": False,
            }
        )
        now = dt.datetime.now(dt.timezone.utc)
        stale = candidate(
            inst_id="STALE-TIME-USDT",
            exchange_timestamp=(now - dt.timedelta(seconds=91)).isoformat(),
            observed_at=now.isoformat(),
        )
        fresh = candidate(
            inst_id="FRESH-TIME-USDT",
            exchange_timestamp=(now - dt.timedelta(seconds=30)).isoformat(),
            observed_at=now.isoformat(),
        )

        with memory_db() as conn:
            generated, report = generate_strategy_lab_candidates(
                conn, settings, [], [stale, fresh]
            )
            stored = conn.execute(
                "select inst_id,observed_at from strategy_feature_snapshots"
            ).fetchall()

        self.assertEqual([], generated)
        self.assertEqual(["FRESH-TIME-USDT"], [row["inst_id"] for row in stored])
        self.assertEqual(1, report["feature_snapshots"]["feature_frames"])
        self.assertEqual(fresh["exchange_timestamp"], stored[0]["observed_at"])

    def test_disabled_lifecycle_previews_promotion_without_queue_or_mutation(self):
        settings = base_settings()
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])
            conn.execute(
                """
                update strategy_lab_experiments
                set created_at = ?, consecutive_passes = 1
                where strategy_lab_id = ?
                """,
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = candidate(
                    inst_id=f"READ-ONLY-{idx}",
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 4.0, 14.0, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, created_at, created_at),
                )
            conn.commit()
            settings["strategy_lab"]["lifecycle_mutations_enabled"] = False
            result = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            row = conn.execute(
                """
                select status, consecutive_passes, promoted_proposal_id
                from strategy_lab_experiments where strategy_lab_id = ?
                """,
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()
            recommendation_table = conn.execute(
                "select count(*) from sqlite_master where type='table' and name='llm_recommendations'"
            ).fetchone()[0]

        self.assertEqual("promotion_gate_passed_read_only", result["decision"])
        self.assertEqual("active_testing", row["status"])
        self.assertEqual(1, row["consecutive_passes"])
        self.assertIsNone(row["promoted_proposal_id"])
        self.assertEqual(0, recommendation_table)

    def test_passing_allowlisted_root_persists_candidate_when_promotion_queue_is_disabled(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "experiment_root_allowlist": ["okx_spot_survivor_lab_v1"],
                "lifecycle_mutations_enabled": True,
                "recommendation_emission_enabled": False,
                "promotion_enabled": False,
                "adaptive_relaxation_enabled": False,
            }
        )
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generate_strategy_lab_candidates(conn, settings, [candidate()])
            conn.execute(
                """
                update strategy_lab_experiments
                set created_at = ?, consecutive_passes = 1
                where strategy_lab_id = ?
                """,
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = candidate(
                    inst_id=f"PROMOTION-CANDIDATE-{idx}",
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                mark_reliable_close(conn, trade_id, created_at)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 4.0, 14.0, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, created_at, created_at),
                )
            conn.commit()

            result = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            row = conn.execute(
                """
                select status, consecutive_passes, promoted_proposal_id
                from strategy_lab_experiments where strategy_lab_id = ?
                """,
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()
            recommendation_table = conn.execute(
                "select count(*) from sqlite_master where type='table' and name='llm_recommendations'"
            ).fetchone()[0]

        self.assertEqual("promotion_candidate", result["decision"])
        self.assertEqual("promotion_candidate", result["status"])
        self.assertEqual("promotion_candidate", row["status"])
        self.assertEqual(2, row["consecutive_passes"])
        self.assertIsNone(row["promoted_proposal_id"])
        self.assertEqual(0, recommendation_table)

    def test_deterministic_recovery_canary_bootstraps_and_compiles_without_recommendation(self):
        settings = base_settings()
        settings["strategy_lab"].update(
            {
                "bootstrap_recovery_canary_enabled": True,
                "experiment_root_allowlist": [RECOVERY_CANARY_STRATEGY_LAB_ID],
                "snapshot_warmup_enabled": False,
                "lifecycle_mutations_enabled": False,
                "recommendation_emission_enabled": False,
                "promotion_enabled": False,
                "adaptive_relaxation_enabled": False,
                "max_candidates_per_loop": 1,
                "max_candidates_per_experiment": 1,
            }
        )
        decision_time = dt.datetime.now(dt.timezone.utc)
        ticker_timestamp_ms = int(decision_time.timestamp() * 1000)
        source = candidate(
            venue="OKX",
            inst_id="BTC-USDT-SWAP",
            direction="short_perp_long_spot",
            trade_type="perp_funding_basis",
            target_surface="perp_funding_basis",
            hedge_venue="OKX_SPOT",
            hedge_instrument="BTC-USDT",
            fee_model="verified_fixture",
            paper_leg_mapping_valid=True,
            venue_capabilities={
                "supports_basis_path": True,
                "supports_basis_carry": True,
                "supports_perpetuals": True,
                "supports_spot_long": True,
            },
            funding_bps=8.0,
            basis_bps=12.0,
            edge_bps_estimate=7.0,
            base_asset="BTC",
            quote_asset="USDT",
            market_surface="okx_perpetual_swap",
        )
        source = apply_paired_direct_entry_contract(
            source,
            {
                "instId": "BTC-USDT-SWAP",
                "bidPx": "100.0",
                "last": "100.0",
                "ts": str(ticker_timestamp_ms),
            },
            {
                "instId": "BTC-USDT",
                "askPx": "100.0",
                "last": "100.0",
                "ts": str(ticker_timestamp_ms),
            },
            settings,
            decision_time=decision_time,
        )
        with memory_db() as conn:
            generated, report = generate_strategy_lab_candidates(conn, settings, [source])
            row = conn.execute(
                """
                select status, compile_status, source_agent, parent_strategy_lab_id
                from strategy_lab_experiments where strategy_lab_id = ?
                """,
                (RECOVERY_CANARY_STRATEGY_LAB_ID,),
            ).fetchone()
            recommendation_table = conn.execute(
                "select count(*) from sqlite_master where type='table' and name='llm_recommendations'"
            ).fetchone()[0]
            entry_contract = generated[0]["paired_direct_v1"]
            self.assertEqual(
                [50.0, 50.0],
                [
                    entry_contract["entry_components"][name]["notional_usd"]
                    for name in ("perp", "spot")
                ],
            )
            entry_event_at = dt.datetime.fromisoformat(
                entry_contract["entry_components"]["perp"]["event_at"]
            )
            target_at = entry_event_at + dt.timedelta(minutes=60)
            exit_at = target_at + dt.timedelta(minutes=1)

            def exit_component(venue, inst_id, surface):
                return {
                    "observation_id": f"{venue}:{inst_id}:{exit_at.isoformat()}",
                    "venue": venue,
                    "inst_id": inst_id,
                    "market_surface": surface,
                    "candle_open_at": target_at.isoformat(),
                    "event_at": exit_at.isoformat(),
                    "received_at": (exit_at + dt.timedelta(seconds=1)).isoformat(),
                    "price": 100.0,
                    "source_kind": "exchange_candle_1m_close",
                    "source_parser": "okx_1m_candles",
                    "source_endpoint": "/api/v5/market/history-candles",
                    "source_event_id": f"{venue}:{inst_id}:{exit_at.isoformat()}",
                    "is_closed": True,
                    "is_partial": False,
                }

            exit_components = {
                "perp": exit_component("OKX", "BTC-USDT-SWAP", "perpetual_swap"),
                "spot": exit_component("OKX_SPOT", "BTC-USDT", "spot"),
            }
            funding = {
                "batch_id": "funding-coverage-canary",
                "coverage_status": "complete",
                "complete_from": entry_event_at.isoformat(),
                "complete_through": exit_at.isoformat(),
                "allow_estimates": False,
                "source": {
                    "name": "OKX public REST realized funding history",
                    "endpoint": "/api/v5/public/funding-rate-history",
                    "parser": "okx_realized_funding_history",
                    "inst_id": "BTC-USDT-SWAP",
                },
                "query": {
                    "query_id": "okx-funding-query-canary",
                    "request_url": "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=400",
                    "requested_from": entry_event_at.isoformat(),
                    "requested_through": exit_at.isoformat(),
                    "received_at": (exit_at + dt.timedelta(seconds=1)).isoformat(),
                    "request_succeeded": True,
                    "http_status": 200,
                    "page_count": 1,
                    "pagination_complete": True,
                    "range_complete": True,
                    "payload_sha256": "a" * 64,
                },
                "events": [],
            }
            paired_label = calculate_paired_direct_outcome(
                generated[0],
                exit_components,
                funding,
                target_at,
                settings=settings,
            )
            self.assertTrue(paired_label["valid"], paired_label.get("reasons"))
            outcome_context = {
                "paper_outcome_measurement_contract": "paired_direct_v1",
                "paired_direct_v1_outcome": paired_label["context"],
                "paper_price_observations": {
                    name: {"observation_id": row["observation_id"]}
                    for name, row in exit_components.items()
                },
            }
            stored_pnl_bps = round(float(paired_label["pnl_bps"]), 3)
            now = exit_at.isoformat()
            for index, (exact_lineage, route_status) in enumerate(
                ((True, "standard"), (False, "standard"), (True, "blocked"))
            ):
                label_candidate = dict(generated[0])
                # Each observed trade is a distinct admission episode.  The
                # recovery queue performs this canonicalization in production;
                # keep the direct evaluator fixture equally unambiguous.
                episode_id = (
                    f"{RECOVERY_CANARY_STRATEGY_LAB_ID}:v1:direct:test:{index}"
                )
                label_candidate["episode_id"] = episode_id
                label_candidate["admission_episode_id"] = episode_id
                label_candidate["paper_admission"] = {
                    **dict(label_candidate.get("paper_admission") or {}),
                    "episode_id": episode_id,
                }
                if not exact_lineage:
                    label_candidate["signal_lineage_key"] = "STRATEGY_LAB|wrong|v1"
                if route_status != "standard":
                    # Even an explicit shared-label override cannot make a
                    # non-standard route count toward recovery promotion.
                    label_candidate["paper_route_status"] = route_status
                    label_candidate["route_status"] = route_status
                    label_candidate["paper_label_eligible"] = True
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": route_status,
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(
                    conn,
                    label_candidate,
                    review,
                    settings=settings,
                )
                mark_reliable_close(conn, trade_id, now)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, 100.0, ?, ?, ?, ?, 60, 'valid', 'paired_direct_v1')
                    """,
                    (
                        trade_id,
                        now,
                        stored_pnl_bps,
                        json.dumps(outcome_context, sort_keys=True),
                        target_at.isoformat(),
                        now,
                    ),
                )
            conn.commit()
            settings["strategy_lab"]["lifecycle_mutations_enabled"] = True
            canary_evaluation = evaluate_strategy_lab(conn, settings)["evaluated"][0]
            stored_canary_evaluation = json.loads(
                conn.execute(
                    "select evaluation_json from strategy_lab_experiments where strategy_lab_id = ?",
                    (RECOVERY_CANARY_STRATEGY_LAB_ID,),
                ).fetchone()["evaluation_json"]
                or "{}"
            )

        fill_settings = copy.deepcopy(settings)
        fill_settings["risk"]["paper_notional_usd"] = 100.0
        ticket = build_order_ticket(
            generated[0],
            {
                "decision": "approve_paper_trade",
                "paper_allocation_multiplier": 1.0,
                "route_status": "standard",
            },
            fill_settings,
        )

        self.assertEqual(1, len(generated), report)
        self.assertEqual(RECOVERY_CANARY_STRATEGY_LAB_ID, generated[0]["strategy_lab_id"])
        self.assertEqual(RECOVERY_CANARY_STRATEGY_LAB_ID, generated[0]["strategy_lab_lineage_root_id"])
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("deterministic_recovery_bootstrap", row["source_agent"])
        self.assertIsNone(row["parent_strategy_lab_id"])
        self.assertEqual("created", report["recovery_canary_bootstrap"]["status"])
        self.assertEqual(0, recommendation_table)
        self.assertEqual(1.0, generated[0]["paper_allocation_multiplier"])
        self.assertNotIn("paper_route_would_block", generated[0])
        self.assertFalse(generated[0]["paper_route_eligibility"]["suppressed"])
        self.assertEqual(100.0, ticket["notional_usd"])
        self.assertEqual(1, canary_evaluation["metrics"]["count"])
        self.assertEqual(
            1,
            stored_canary_evaluation["outcomes"]["label_status_counts"][
                "canary_direct_admission_lineage_mismatch"
            ],
        )
        self.assertEqual(
            1,
            stored_canary_evaluation["outcomes"]["label_status_counts"][
                "paper_label_ineligible:recovery_route_not_standard"
            ],
        )

    def test_summary_reports_recent_experiments(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            risk_rec = lab_rec()
            risk_rec["recommendation_id"] = "rec_risk_filter"
            risk_rec["payload"]["strategy_lab_experiment"]["strategy_lab_id"] = "risk_filter_lab"
            risk_rec["payload"]["strategy_lab_experiment"]["experiment_type"] = "risk_filter"
            risk_rec["payload"]["strategy_lab_experiment"]["hypothesis"] = "Cooldown weak entries after repeated decay."
            ingest_strategy_lab_recommendation(conn, risk_rec)
            summary = strategy_lab_summary(conn)
            self.assertEqual(2, summary["total_experiments"])
            self.assertEqual(1, len(summary["recent_market_strategies"]))
            self.assertEqual(1, len(summary["recent_non_market_experiments"]))
            self.assertEqual("market_strategy", summary["recent_market_strategies"][0]["experiment_type"])

    def test_report_splits_market_and_non_market_experiments(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            repair_rec = {
                "recommendation_id": "rec_report_repair",
                "payload": {
                    "action": "propose_strategy_lab_experiment",
                    "title": "Repair malformed JSON recommendation output",
                    "rationale": "Parser failures should not appear as strategies.",
                },
            }
            ingest_strategy_lab_recommendation(conn, repair_rec)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                with mock.patch("strategy_lab.REPORT_JSON", tmp_path / "strategy_lab_report.json"), (
                    mock.patch("strategy_lab.REPORT_MD", tmp_path / "strategy_lab_report.md")
                ):
                    report = write_strategy_lab_reports(conn)
            repair_row = conn.execute(
                "select experiment_type from strategy_lab_experiments where strategy_lab_id like 'repair_malformed%'"
            ).fetchone()

        self.assertEqual(1, len(report["summary"]["recent_market_strategies"]))
        self.assertEqual(1, len(report["summary"]["recent_non_market_experiments"]))
        self.assertEqual("system_repair", repair_row["experiment_type"])

    def test_report_summary_uses_current_generation_count(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                with mock.patch("strategy_lab.REPORT_JSON", tmp_path / "strategy_lab_report.json"), (
                    mock.patch("strategy_lab.REPORT_MD", tmp_path / "strategy_lab_report.md")
                ):
                    report = write_strategy_lab_reports(conn, {"generated_candidates": 6})

        self.assertEqual(6, report["summary"]["generated_candidates_last_cycle"])

    def test_report_flags_reflect_disabled_master_and_runtime_controls(self):
        settings = base_settings()
        settings["strategy_lab"]["enabled"] = False
        with memory_db() as conn:
            generated, generation = generate_strategy_lab_candidates(
                conn,
                settings,
                [candidate()],
            )
            evaluation = evaluate_strategy_lab(conn, settings)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                with mock.patch("strategy_lab.REPORT_JSON", tmp_path / "strategy_lab_report.json"), (
                    mock.patch("strategy_lab.REPORT_MD", tmp_path / "strategy_lab_report.md")
                ):
                    report = write_strategy_lab_reports(conn, generation, evaluation)

        self.assertEqual([], generated)
        self.assertFalse(report["summary"]["enabled"])
        self.assertFalse(report["controls"]["master_enabled"])
        self.assertFalse(report["controls"]["runtime_generation_enabled"])
        self.assertFalse(report["controls"]["evaluation_enabled"])


if __name__ == "__main__":
    unittest.main()
