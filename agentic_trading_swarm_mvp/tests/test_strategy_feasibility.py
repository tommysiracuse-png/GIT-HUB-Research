from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import dynamic_agents
import storage
from strategy_feasibility import (
    maybe_create_relaxed_child,
    profile_observation_program,
    record_contract_evaluation,
)
from strategy_program import generate_program_candidates


def program() -> dict:
    return {
        "type": "observation_program",
        "universe": {"venues": ["TEST"]},
        "entry_expression": (
            "return_60m_bps >= 10 and quality_score >= 60 and "
            "liquidity_score >= 0.7 and spread_bps <= 8"
        ),
        "invalidation_expression": "False",
        "direction": "long",
        "edge_expression": "return_60m_bps - 5",
        "score_expression": "quality_score",
        "route_surface": "proxy",
    }


def frames(count: int = 250) -> list[dict]:
    return [
        {
            "venue": "TEST", "inst_id": f"T{index}", "trade_type": "global_market_discovery_proxy",
            "last": 100 + index / 100, "return_60m_bps": 20, "quality_score": 50,
            "liquidity_score": 0.8, "spread_bps": 5, "stale_minutes": 0.2,
            "session_status": "open", "observed_at": "2026-08-04T12:00:00+00:00",
        }
        for index in range(count)
    ]


class StrategyFeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage.init_db(self.conn)
        self.settings = {
            "strategy_lab": {
                "max_candidates_per_experiment": 10,
                "adaptive_relaxation": {
                    "enabled": True, "min_eligible_scans": 6,
                    "min_eligible_observations": 250, "max_gates_per_revision": 3,
                    "minimum_gate_percentile": 0.25, "maximum_gate_percentile": 0.75,
                    "paper_allocation_multiplier": 0.10,
                },
            }
        }
        now = storage.utc_now()
        self.experiment = {
            "strategy_lab_id": "impossible_quality", "version": 1,
            "hypothesis": "Test a quality-gated reversal.", "strategy_logic": program(),
            "risk_gates": {}, "promotion_rules": {}, "data_requirements": {},
            "source_surface": "test_proxy", "permitted_target_surface": ["proxy"],
            "surface_policy": {}, "source_recommendation_id": "rec-1",
        }
        self.conn.execute(
            """
            insert into strategy_lab_experiments(
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                original_strategy_logic_json,compiled_strategy_logic_json,compile_status,
                data_requirements_json,risk_gates_json,promotion_rules_json,source_agent,
                source_recommendation_id,created_at,updated_at,source_surface,
                permitted_target_surfaces_json,surface_policy_json
            ) values(?,1,'market_strategy','active_testing',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.experiment["strategy_lab_id"], self.experiment["hypothesis"],
                json.dumps(program()), json.dumps(program()), json.dumps(program()), "compiled",
                "{}", "{}", "{}", "test", "rec-1", now, now, "test_proxy", '["proxy"]', "{}",
            ),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_impossible_threshold_is_profiled_and_relaxed_child_trades(self):
        profile = profile_observation_program(self.experiment, frames(), self.settings)
        self.assertEqual("impossible_threshold", profile["feasibility_status"])
        quality = next(item for item in profile["relaxation"]["changes"] if "quality_score" in item["names"])
        self.assertEqual(50.0, quality["relaxed_threshold"])

        record_contract_evaluation(self.conn, self.experiment, profile, cycle_id="cycle-1")
        child = maybe_create_relaxed_child(self.conn, self.experiment, profile, self.settings)
        self.conn.commit()
        self.assertEqual("created", child["status"])
        row = self.conn.execute(
            "select * from strategy_lab_experiments where strategy_lab_id=?", (child["strategy_lab_id"],)
        ).fetchone()
        child_experiment = {
            "strategy_lab_id": row["strategy_lab_id"], "version": row["version"],
            "hypothesis": row["hypothesis"], "strategy_logic": json.loads(row["strategy_logic_json"]),
            "risk_gates": json.loads(row["risk_gates_json"]),
        }
        candidates, diagnostic = generate_program_candidates(child_experiment, frames(2), self.settings)
        self.assertGreater(diagnostic["generated_candidate_count"], 0)
        self.assertEqual(0.10, candidates[0]["strategy_reliability_allocation_multiplier"])
        self.assertEqual("impossible_quality", row["parent_strategy_lab_id"])

    def test_unresolved_safety_or_feature_gate_prevents_useless_relaxed_child(self):
        safety_experiment = dict(self.experiment)
        safety_experiment["strategy_logic"] = {
            **program(),
            "entry_expression": "quality_score >= 60 and stale_minutes <= 5",
        }
        stale_frames = [{**frame, "stale_minutes": 20} for frame in frames()]
        profile = profile_observation_program(safety_experiment, stale_frames, self.settings)
        self.assertEqual("blocked_observation_safety", profile["feasibility_status"])
        self.assertFalse(profile["relaxation"]["complete_repair"])
        record_contract_evaluation(self.conn, self.experiment, profile, cycle_id="safety-cycle")
        self.assertIsNone(maybe_create_relaxed_child(self.conn, safety_experiment, profile, self.settings))

        feature_experiment = dict(self.experiment)
        feature_experiment["strategy_logic"] = {
            **program(),
            "entry_expression": "quality_score >= 60 and return_5m_bps * return_60m_bps < 0",
        }
        profile = profile_observation_program(feature_experiment, frames(), self.settings)
        self.assertEqual("missing_feature_history", profile["feasibility_status"])
        self.assertIn("missing_feature_history", {item["reason"] for item in profile["blocking_gates"]})
        self.assertFalse(profile["relaxation"]["complete_repair"])

    def test_adaptive_relaxation_stops_repeating_the_same_lineage(self):
        self.settings["strategy_lab"]["adaptive_relaxation"]["max_lineage_depth"] = 1
        profile = profile_observation_program(self.experiment, frames(), self.settings)
        record_contract_evaluation(
            self.conn, self.experiment, profile, cycle_id="depth-cycle"
        )
        child = maybe_create_relaxed_child(self.conn, self.experiment, profile, self.settings)
        self.conn.commit()
        row = self.conn.execute(
            "select * from strategy_lab_experiments where strategy_lab_id=?",
            (child["strategy_lab_id"],),
        ).fetchone()
        child_experiment = {
            "strategy_lab_id": row["strategy_lab_id"],
            "parent_strategy_lab_id": row["parent_strategy_lab_id"],
            "strategy_logic": json.loads(row["strategy_logic_json"]),
            "risk_gates": json.loads(row["risk_gates_json"]),
        }

        blocked = maybe_create_relaxed_child(
            self.conn, child_experiment, profile, self.settings
        )

        self.assertEqual("lineage_depth_reached", blocked["status"])
        self.assertEqual(1, blocked["lineage_depth"])

    def test_architect_creates_a_child_after_repeated_gap(self):
        now = storage.utc_now()
        for index in range(3):
            self.conn.execute(
                """insert into strategy_lab_experiments(
                strategy_lab_id,version,experiment_type,status,hypothesis,strategy_logic_json,
                data_requirements_json,risk_gates_json,promotion_rules_json,created_at,updated_at
                ) values(?,1,'market_strategy','rejected_invalid',?,'{}','{}','{}','{}',?,?)""",
                (f"bad-{index}", f"Invalid hypothesis {index}", now, now),
            )
        self.conn.commit()
        settings = {"dynamic_agents": {"enabled": True, "bootstrap_seed_agents": False}}
        first = dynamic_agents.prepare_dynamic_agent_cycle(self.conn, {"strategy_lab": {"invalid": 3}}, settings, "cycle-1")
        second = dynamic_agents.prepare_dynamic_agent_cycle(self.conn, {"strategy_lab": {"invalid": 3}}, settings, "cycle-2")
        self.assertIsNone(first["agent_architect"]["spawn_candidate"])
        recommendation = dynamic_agents.architect_recommendation(second)
        self.assertEqual("spawn_agent", recommendation["action"])
        registered = dynamic_agents.ingest_spawn_agent_recommendation(
            self.conn, recommendation, recommendation_id="spawn-rec"
        )
        self.assertEqual("created", registered["status"])
        row = self.conn.execute("select generation from agent_specs where agent_id=?", (registered["agent_id"],)).fetchone()
        self.assertEqual(1, row["generation"])


if __name__ == "__main__":
    unittest.main()
