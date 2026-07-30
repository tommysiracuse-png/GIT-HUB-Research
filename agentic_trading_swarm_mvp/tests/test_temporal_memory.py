from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import llm_swarm_runner  # noqa: E402
from storage import (  # noqa: E402
    add_code_evolution_proposal,
    add_llm_recommendation,
    connect,
    update_code_evolution_proposal,
)
from temporal_memory import (  # noqa: E402
    bootstrap_legacy_memory,
    graphiti_status,
    record_swarm_reflection,
    refresh_evidence_memories,
    retrieve_role_memories,
    upsert_memory_fact,
)


class TemporalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_repeated_fact_reinforces_and_material_change_versions(self) -> None:
        first = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|carry",
            "has_reliable_60m_outcomes",
            "avg=20",
            0.9,
            "test",
            {},
            observed_at="2026-07-01T00:00:00+00:00",
            profile_version_hours=6,
        )
        reinforced = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|carry",
            "has_reliable_60m_outcomes",
            "avg=20",
            0.9,
            "test",
            {},
            observed_at="2026-07-01T01:00:00+00:00",
            profile_version_hours=6,
        )
        updated = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|carry",
            "has_reliable_60m_outcomes",
            "avg=22",
            0.9,
            "test",
            {},
            observed_at="2026-07-01T02:00:00+00:00",
            profile_version_hours=6,
        )
        versioned = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|carry",
            "has_reliable_60m_outcomes",
            "avg=-12",
            0.9,
            "test",
            {},
            observed_at="2026-07-01T07:00:00+00:00",
            profile_version_hours=6,
        )

        self.assertEqual(first["memory_id"], reinforced["memory_id"])
        self.assertEqual(updated["operation"], "updated_profile")
        self.assertNotEqual(versioned["memory_id"], first["memory_id"])
        rows = self.conn.execute(
            "select status, version, observation_count from temporal_memories order by version"
        ).fetchall()
        self.assertEqual([(row["status"], row["version"]) for row in rows], [("superseded", 1), ("active", 2)])
        self.assertEqual(rows[0]["observation_count"], 3)

    def test_role_retrieval_is_relevant_diverse_and_temporal(self) -> None:
        for index in range(10):
            upsert_memory_fact(
                self.conn,
                "venue_health",
                f"VENUE_{index}",
                "is_reachable",
                "public adapter healthy",
                0.9,
                "market_discovery",
                {},
                namespace="markets",
                importance=0.8,
                commit=False,
            )
            upsert_memory_fact(
                self.conn,
                "code_evolution_outcome",
                f"PATCH_{index}",
                "promoted",
                "tests passed and runtime integrated",
                0.9,
                "code_evolution",
                {},
                namespace="code",
                importance=0.8,
                commit=False,
            )
        upsert_memory_fact(
            self.conn,
            "signal_performance",
            "FRONTIER|LONG",
            "has_reliable_60m_outcomes",
            "positive",
            0.9,
            "paper_outcome_engine",
            {},
            namespace="outcomes",
            observed_at="2026-07-01T00:00:00+00:00",
            profile_version_hours=1,
            commit=False,
        )
        upsert_memory_fact(
            self.conn,
            "signal_performance",
            "FRONTIER|LONG",
            "has_reliable_60m_outcomes",
            "decayed",
            0.9,
            "paper_outcome_engine",
            {},
            namespace="outcomes",
            observed_at="2026-07-01T02:00:00+00:00",
            profile_version_hours=1,
            commit=False,
        )
        self.conn.commit()
        settings = {
            "agent_memory": {
                "enabled": True,
                "retrieval_limit_per_agent": 8,
                "retrieval_candidate_pool": 40,
                "historical_context_fraction": 0.25,
            }
        }
        packet = {
            "signal_stats": [{"signal_key": "FRONTIER|LONG"}],
            "improvement_tasks": [{"title": "public adapter market coverage"}],
        }
        scout = retrieve_role_memories(self.conn, packet, "market_scout", settings, cycle_id="cycle")
        builder = retrieve_role_memories(self.conn, packet, "build_planner", settings, cycle_id="cycle")

        self.assertEqual(len(scout), 8)
        self.assertEqual(len(builder), 8)
        self.assertEqual(scout[0]["namespace"], "markets")
        self.assertEqual(builder[0]["namespace"], "code")
        self.assertNotEqual(
            [item["memory_id"] for item in scout[:3]],
            [item["memory_id"] for item in builder[:3]],
        )
        self.assertTrue(any(item["temporal_relation"] == "previous_version" for item in scout + builder))
        self.assertTrue(all(len(item["summary"]) <= 800 for item in scout + builder))

    def test_role_namespace_reservations_prevent_high_score_crowding(self) -> None:
        for index in range(200):
            upsert_memory_fact(
                self.conn,
                "strategy_lab_evaluation",
                f"STRATEGY_{index}",
                "promote_candidate",
                "high scoring strategy evidence",
                0.99,
                "strategy_lab",
                {},
                namespace="strategies",
                importance=1.0,
                outcome_score=1.0,
                commit=False,
            )
        for index in range(8):
            upsert_memory_fact(
                self.conn,
                "venue_health",
                f"VENUE_{index}",
                "is_reachable",
                "public venue market adapter",
                0.8,
                "market_discovery",
                {},
                namespace="markets",
                importance=0.45,
                commit=False,
            )
        upsert_memory_fact(
            self.conn,
            "route_resolver",
            "execution_routes",
            "has_route_summary",
            "route requirements",
            0.8,
            "route_resolver",
            {},
            namespace="routes",
            importance=0.45,
            commit=False,
        )
        self.conn.commit()
        settings = {
            "agent_memory": {
                "enabled": True,
                "retrieval_limit_per_agent": 12,
                "retrieval_candidate_pool": 40,
                "preferred_namespace_fraction": 0.67,
            }
        }
        scout = retrieve_role_memories(self.conn, {}, "market_scout", settings, cycle_id="scout")
        route = retrieve_role_memories(self.conn, {}, "execution_route_hunter", settings, cycle_id="route")

        self.assertGreaterEqual(sum(item["namespace"] == "markets" for item in scout), 4)
        self.assertTrue(any(item["namespace"] == "routes" for item in route))
        self.assertGreaterEqual(sum(item["namespace"] == "markets" for item in route), 3)

    def test_reflection_links_memory_and_outcomes_update_utility(self) -> None:
        memory = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|funding",
            "has_reliable_60m_outcomes",
            "positive carry",
            0.95,
            "paper_outcome_engine",
            {},
            namespace="outcomes",
            importance=0.9,
        )
        recommendation = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Use carry evidence",
            "rationale": "Outcome-backed improvement",
            "proposed_change": "Improve paper carry scoring.",
            "agent_name": "build_planner",
        }
        state = {
            "agent_outputs": [
                {
                    "agent_name": "build_planner",
                    "accepted": True,
                    "parse_status": "native_valid",
                    "recommendation": recommendation,
                    "memory_ids": [memory["memory_id"]],
                }
            ],
            "graph_trace": [],
        }
        record_swarm_reflection(self.conn, state, "cycle-1", {"agent_memory": {"enabled": True}})
        recommendation_id = hashlib.sha256(json.dumps(recommendation, sort_keys=True).encode("utf-8")).hexdigest()
        add_llm_recommendation(
            self.conn,
            recommendation_id,
            recommendation["action"],
            recommendation["title"],
            recommendation["rationale"],
            recommendation,
        )
        add_code_evolution_proposal(
            self.conn,
            "proposal-1",
            recommendation_id,
            "build_planner",
            "test-model",
            "standard",
            None,
            "Carry patch",
            "paper_scoring_logic",
            90,
            {},
            {},
        )
        update_code_evolution_proposal(self.conn, "proposal-1", status="promoted", candidate_commit="abc123")
        refresh_evidence_memories(
            self.conn,
            {"agent_memory": {"enabled": True, "legacy_bootstrap_enabled": False}},
        )

        row = self.conn.execute(
            "select utility_score, success_count, failure_count from temporal_memories where memory_id = ?",
            (memory["memory_id"],),
        ).fetchone()
        self.assertGreater(row["utility_score"], 0)
        self.assertEqual(row["success_count"], 1)
        self.assertEqual(row["failure_count"], 0)

    def test_legacy_bootstrap_collapses_duplicate_rows(self) -> None:
        for index in range(20):
            self.conn.execute(
                "insert into memory_facts(created_at,fact_type,subject,predicate,object,confidence,source,metadata_json) "
                "values (?,?,?,?,?,?,?,?)",
                (
                    f"2026-07-01T00:{index:02d}:00+00:00",
                    "signal_stat",
                    f"S{index % 2}",
                    "has_score_adjustment",
                    str(index),
                    0.9,
                    "legacy",
                    "{}",
                ),
            )
        self.conn.commit()
        result = bootstrap_legacy_memory(
            self.conn,
            {"agent_memory": {"legacy_bootstrap_enabled": True, "legacy_bootstrap_limit": 100}},
        )
        self.assertEqual(result["imported"], 2)
        self.assertEqual(self.conn.execute("select count(*) from temporal_memories").fetchone()[0], 2)

    def test_graphiti_waits_for_backend_without_importing_runtime(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            status = graphiti_status({"agent_memory": {"graphiti": {"mode": "auto"}}})
        self.assertEqual(status["status"], "waiting_for_graph_backend")


class LangGraphMemoryTests(unittest.TestCase):
    def test_role_specific_memory_reaches_each_agent_and_checkpoint_is_explicit(self) -> None:
        packet = {"allowed_recommendation_actions": ["propose_hunter_directive"]}
        role_memory = {
            agent["name"]: [{"memory_id": f"mem-{agent['name']}", "summary": agent["name"]}]
            for agent in llm_swarm_runner.AGENTS
        }
        observed: dict[str, str] = {}

        def fake_agent(agent: dict, _packet: dict, memory: list[dict]) -> dict:
            observed[agent["name"]] = memory[0]["memory_id"]
            return {
                "action": "propose_hunter_directive",
                "priority": 60,
                "title": agent["name"],
                "rationale": "test",
                "market_key": agent["name"],
                "evidence": {},
                "proposed_change": "test",
                "agent_name": agent["name"],
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            llm_swarm_runner, "run_agent", side_effect=fake_agent
        ):
            llm_swarm_runner.run_langgraph_if_available(
                packet,
                role_memory,
                {
                    "agent_memory": {
                        "checkpoint_enabled": True,
                        "checkpoint_path": str(pathlib.Path(tmp) / "checkpoints.sqlite"),
                    }
                },
                "cycle-test",
            )

        self.assertEqual(set(observed), {agent["name"] for agent in llm_swarm_runner.AGENTS})
        for agent in llm_swarm_runner.AGENTS:
            self.assertEqual(observed[agent["name"]], f"mem-{agent['name']}")
        self.assertIn(
            llm_swarm_runner.LAST_SWARM_STATE["checkpoint"]["status"],
            {"saved", "package_missing"},
        )


if __name__ == "__main__":
    unittest.main()
