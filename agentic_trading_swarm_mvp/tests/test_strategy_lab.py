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
from settings import DEFAULT_SETTINGS
from storage import init_db, open_paper_trade, save_opportunity, signal_key
from strategy_lab import (
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


class StrategyLabTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
