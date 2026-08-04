from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS  # noqa: E402
from radar_loop import _select_runtime_strategy_lab_candidates  # noqa: E402
from storage import init_db  # noqa: E402
from strategy_lab import (  # noqa: E402
    _queue_promotion,
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
)
from strategy_program import (  # noqa: E402
    ProgramValidationError,
    assert_plugin_parity,
    compile_observation_program,
    evaluate_expression,
    generate_program_candidates,
    novelty_signature,
    record_feature_snapshots,
)


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings() -> dict:
    output = copy.deepcopy(DEFAULT_SETTINGS)
    output["allow_live_trading"] = False
    output["strategy_lab"]["feature_snapshot_max_rows"] = 2_000_000
    return output


def program_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {"venues": ["YAHOO_PROXY"], "asset_classes": ["equity"]},
        "calculated_features": {
            "cost_adjusted_momentum": "return_5m_bps - spread_bps",
        },
        "entry_expression": "quality_score >= 60 and cost_adjusted_momentum > 5",
        "invalidation_expression": "stale_minutes > 5",
        "long_expression": "cost_adjusted_momentum > 0",
        "short_expression": "cost_adjusted_momentum < -20",
        "edge_expression": "max(cost_adjusted_momentum, 0)",
        "score_expression": "clip(50 + cost_adjusted_momentum / 2, 0, 100)",
        "route_surface": "proxy",
    }


def shock_reversal_logic() -> dict:
    return {
        "type": "observation_program",
        "universe": {"venues": ["YAHOO_PROXY"]},
        "calculated_features": {
            "shock_magnitude_bps": "abs(return_60m_bps)",
            "shock_sigma": "abs(return_60m_bps) / max(volatility_60m_bps, 10)",
            "flip_strength_bps": "max(0, -(return_5m_bps * return_60m_bps) / max(abs(return_60m_bps), 1))",
            "cost_adjusted_reversal_edge_bps": "max(0, min(0.15 * abs(return_60m_bps) + flip_strength_bps, 40) - 2 * spread_bps)",
        },
        "entry_expression": (
            "shock_magnitude_bps >= 40 and shock_sigma >= 1.75 "
            "and return_5m_bps * return_60m_bps < 0 and flip_strength_bps >= 5 "
            "and spread_bps <= 8 and liquidity_score >= 0.65 "
            "and quality_score >= 60 and stale_minutes <= 5"
        ),
        "invalidation_expression": (
            "shock_magnitude_bps < 25 or return_5m_bps * return_60m_bps >= 0 "
            "or spread_bps > 12 or quality_score < 55 or stale_minutes > 10"
        ),
        "long_expression": "return_60m_bps < 0 and return_5m_bps > 0",
        "short_expression": "return_60m_bps > 0 and return_5m_bps < 0",
        "edge_expression": "cost_adjusted_reversal_edge_bps",
        "score_expression": "clip(30 + 12 * min(shock_sigma, 4) + min(flip_strength_bps, 20), 0, 100)",
        "route_surface": "proxy",
        "output_trade_type": "global_proxy_shock_reversal",
    }


def lab_recommendation(strategy_lab_id: str = "observation_momentum_v1", logic: dict | None = None) -> dict:
    return {
        "recommendation_id": "rec_" + strategy_lab_id,
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "title": "Test observation-native cost-adjusted momentum",
            "rationale": "Test a reusable price-history hypothesis without depending on scanner candidates.",
            "strategy_lab_experiment": {
                "strategy_lab_id": strategy_lab_id,
                "version": 1,
                "experiment_type": "market_strategy",
                "hypothesis": "Liquid instruments with fresh quality-confirmed momentum continue after costs.",
                "source_surface": "proxy",
                "permitted_target_surface": ["proxy"],
                "strategy_logic": logic or program_logic(),
                "data_requirements": {"paper_only": True},
                "risk_gates": {},
                "promotion_rules": {},
            },
        },
    }


def observation(price: float, observed_at: str) -> dict:
    return {
        "inst_id": "TEST:ABC",
        "venue": "YAHOO_PROXY",
        "trade_type": "global_market_discovery_proxy",
        "market_type": "equity",
        "asset_class": "equity",
        "region": "global",
        "last": price,
        "spread_bps": 2.0,
        "liquidity_score": 0.8,
        "quality_score": 80.0,
        "quality_status": "verified",
        "stale_minutes": 0.0,
        "observed_at": observed_at,
        "price_source": "fixture",
    }


class StrategyProgramTests(unittest.TestCase):
    def test_safe_expression_rejects_code_and_attribute_access(self) -> None:
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("__import__('os').system('whoami')", {})
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("last.__class__", {"last": 1.0})
        with self.assertRaises(ProgramValidationError):
            evaluate_expression("10 ** 1000000", {})
        self.assertEqual(12.0, evaluate_expression("clip(last + 2, 0, 20)", {"last": 10.0}))

    def test_output_trade_type_is_bounded_to_the_proxy_shock_reversal_family(self) -> None:
        logic = shock_reversal_logic()
        program, diagnostic = compile_observation_program(logic)
        self.assertIsNotNone(program, diagnostic)
        self.assertEqual("global_proxy_shock_reversal", program["output_trade_type"])
        self.assertNotEqual(
            novelty_signature(logic),
            novelty_signature({**logic, "output_trade_type": ""}),
        )

        unsupported = copy.deepcopy(logic)
        unsupported["output_trade_type"] = "model_chosen_family"
        program, diagnostic = compile_observation_program(unsupported)
        self.assertIsNone(program)
        self.assertEqual("unsupported_output_trade_type", diagnostic["reason"])

        wrong_surface = copy.deepcopy(logic)
        wrong_surface["route_surface"] = "spot"
        program, diagnostic = compile_observation_program(wrong_surface)
        self.assertIsNone(program)
        self.assertEqual("output_trade_type_requires_proxy_route_surface", diagnostic["reason"])

        mislabeled_continuation = copy.deepcopy(logic)
        mislabeled_continuation["long_expression"] = "return_60m_bps > 0 and return_5m_bps > 0"
        program, diagnostic = compile_observation_program(mislabeled_continuation)
        self.assertIsNone(program)
        self.assertEqual("shock_reversal_invalid_long_expression", diagnostic["reason"])

    def test_shock_reversal_output_preserves_source_lineage_and_emits_both_sides(self) -> None:
        base = {
            **observation(100.0, dt.datetime.now(dt.timezone.utc).isoformat()),
            "volatility_60m_bps": 20.0,
            "quality_score": 80.0,
            "liquidity_score": 0.8,
            "spread_bps": 2.0,
            "stale_minutes": 1.0,
            "provider_age_seconds": 60.0,
            "quote_volume_24h": 2_000_000.0,
            "data_status": "reachable",
        }
        frames = [
            {**base, "inst_id": "DOWN", "return_60m_bps": -80.0, "return_5m_bps": 10.0},
            {**base, "inst_id": "UP", "return_60m_bps": 80.0, "return_5m_bps": -10.0},
            {**base, "inst_id": "NO_FLIP", "return_60m_bps": 80.0, "return_5m_bps": 10.0},
        ]
        experiment = {
            "strategy_lab_id": "shock_reversal",
            "version": 1,
            "hypothesis": "Extreme proxy shocks reverse after a five-minute flip.",
            "strategy_logic": shock_reversal_logic(),
        }

        generated, diagnostic = generate_program_candidates(experiment, frames, settings())

        self.assertEqual(2, len(generated), diagnostic)
        self.assertEqual({"long_proxy", "short_proxy"}, {row["direction"] for row in generated})
        self.assertTrue(all(row["trade_type"] == "global_proxy_shock_reversal" for row in generated))
        self.assertTrue(all(row["strategy_lab_source_trade_type"] == "global_market_discovery_proxy" for row in generated))
        self.assertTrue(all(row["proxy_depth_notional_usd"] == 2_000_000.0 for row in generated))
        self.assertTrue(all(row["freshness_age_seconds"] == 60.0 for row in generated))

    def test_snapshot_store_uses_five_minute_buckets_and_enforces_cap(self) -> None:
        cfg = settings()
        cfg["strategy_lab"]["feature_snapshot_max_rows"] = 2
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        with memory_db() as conn:
            for offset, price in ((-15, 100.0), (-10, 101.0), (-5, 102.0)):
                record_feature_snapshots(
                    conn,
                    [observation(price, (now + dt.timedelta(minutes=offset)).isoformat())],
                    cfg,
                )
            rows = conn.execute(
                "select bucket_at, last from strategy_feature_snapshots order by bucket_at"
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual([101.0, 102.0], [row["last"] for row in rows])
        self.assertTrue(all(dt.datetime.fromisoformat(row["bucket_at"]).minute % 5 == 0 for row in rows))

    def test_observation_program_generates_without_existing_scanner_candidate(self) -> None:
        cfg = settings()
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_recommendation())
            record_feature_snapshots(
                conn,
                [observation(100.0, (now - dt.timedelta(minutes=5)).isoformat())],
                cfg,
            )
            generated, report = generate_strategy_lab_candidates(
                conn,
                cfg,
                [],
                {"TEST:ABC": observation(101.0, now.isoformat())},
            )
            row = conn.execute(
                "select compile_status, novelty_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("observation_momentum_v1",),
            ).fetchone()
        self.assertEqual(1, len(generated), report)
        self.assertEqual("observation_program", generated[0]["strategy_lab_logic_type"])
        self.assertEqual("long_proxy", generated[0]["direction"])
        self.assertEqual("global_market_discovery_proxy", generated[0]["trade_type"])
        self.assertGreater(generated[0]["edge_bps_estimate"], 90)
        self.assertEqual("compiled", row["compile_status"])
        self.assertEqual("novel", row["novelty_status"])
        self.assertEqual(0, report["source_candidate_count"])

    def test_radar_runtime_selection_admits_observation_program_candidates(self) -> None:
        candidate = {
            "strategy_lab_id": "observation_runtime",
            "strategy_lab_logic_type": "observation_program",
            "venue": "YAHOO_PROXY",
            "inst_id": "TEST:ABC",
            "direction": "long_proxy",
            "trade_type": "global_market_discovery_proxy",
            "score": 70.0,
            "strategy_lab_surface_policy": {"eligible": True, "reason": "surface_compatible"},
        }
        selected, summary = _select_runtime_strategy_lab_candidates([candidate], settings())
        self.assertEqual([candidate], selected)
        self.assertEqual(1, summary["selected_count"])

    def test_missing_feature_creates_code_evolution_recommendation(self) -> None:
        logic = program_logic()
        logic["calculated_features"] = {"surprise": "sentiment_surprise * return_5m_bps"}
        logic["entry_expression"] = "surprise > 5"
        logic["long_expression"] = "surprise > 0"
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(
                conn,
                lab_recommendation("needs_sentiment_feature", logic),
            )
            generate_strategy_lab_candidates(
                conn,
                settings(),
                [],
                {"TEST:ABC": observation(101.0, dt.datetime.now(dt.timezone.utc).isoformat())},
            )
            experiment = conn.execute(
                "select status, compile_status, compile_diagnostics_json from strategy_lab_experiments where strategy_lab_id = ?",
                ("needs_sentiment_feature",),
            ).fetchone()
            rec = conn.execute(
                "select action, payload_json from llm_recommendations where recommendation_id like 'strategy_lab_feature_extension_%'"
            ).fetchone()
        self.assertEqual("needs_data", experiment["status"])
        self.assertEqual("needs_data", experiment["compile_status"])
        self.assertEqual("propose_code_change", rec["action"])
        self.assertIn("sentiment_surprise", json.loads(rec["payload_json"])["evidence"]["missing_features"])

    def test_canonical_signature_deduplicates_equivalent_programs(self) -> None:
        first = program_logic()
        second = copy.deepcopy(first)
        second["universe"] = {"asset_classes": ["equity"], "venues": ["yahoo_proxy"]}
        self.assertEqual(novelty_signature(first), novelty_signature(second))
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_recommendation("novel_first", first))
            ingest_strategy_lab_recommendation(conn, lab_recommendation("duplicate_second", second))
            row = conn.execute(
                "select status, novelty_status from strategy_lab_experiments where strategy_lab_id = ?",
                ("duplicate_second",),
            ).fetchone()
        self.assertEqual("rejected_invalid", row["status"])
        self.assertEqual("duplicate_experiment", row["novelty_status"])

    def test_observation_promotion_targets_generated_plugin_and_parity_test(self) -> None:
        experiment = {
            "strategy_lab_id": "observation_momentum_v1",
            "version": 1,
            "experiment_type": "market_strategy",
            "hypothesis": "Fresh momentum continues after costs.",
            "strategy_logic": program_logic(),
            "risk_gates": {},
            "novelty_signature": novelty_signature(program_logic()),
        }
        with memory_db() as conn:
            rec_id = _queue_promotion(conn, experiment, {"metrics": {"count": 30}}, {})
            payload = json.loads(
                conn.execute(
                    "select payload_json from llm_recommendations where recommendation_id = ?",
                    (rec_id,),
                ).fetchone()["payload_json"]
            )
        files = payload["code_change"]["expected_files"]
        self.assertIn("src/signals/generated/observation_momentum_v1.py", files)
        self.assertIn("tests/test_generated_strategy_parity.py", files)
        self.assertIn("reproduce", payload["proposed_change"]["promotion_target"]["parity_requirement"])

    def test_plugin_parity_helper_compares_interpreter_candidates(self) -> None:
        cfg = settings()
        experiment = {
            "strategy_lab_id": "parity_lab",
            "version": 1,
            "hypothesis": "Quality momentum",
            "strategy_logic": {
                **program_logic(),
                "entry_expression": "quality_score >= 60",
                "long_expression": "True",
                "short_expression": "False",
                "edge_expression": "10",
            },
        }
        frames = [observation(101.0, dt.datetime.now(dt.timezone.utc).isoformat())]

        class Plugin:
            @staticmethod
            def generate(_observations, context=None):
                candidates, _ = generate_program_candidates(
                    context["strategy_lab_experiment"],
                    context["feature_frames"],
                    context["settings"],
                )
                return candidates

        assert_plugin_parity(Plugin, experiment, frames, cfg)


if __name__ == "__main__":
    unittest.main()
