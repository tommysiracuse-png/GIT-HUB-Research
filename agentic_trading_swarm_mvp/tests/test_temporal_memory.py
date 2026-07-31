from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
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
    refresh_memory_prose,
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

    def test_signal_performance_memory_is_descriptive_prose(self) -> None:
        evidence = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "funding_capture_long_perp",
            "horizon_minutes": 60,
            "valid_labels": 57,
            "avg_pnl_bps": 31.1,
            "win_rate": 0.571,
            "worst_decile_bps": -38.4,
            "best_bps": 181.2,
            "worst_bps": -74.8,
        }
        upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|perp_funding_basis|funding_capture_long_perp|standard",
            "has_reliable_60m_outcomes",
            json.dumps(evidence),
            0.95,
            "paper_outcome_engine",
            evidence,
            namespace="outcomes",
            outcome=evidence,
        )

        memory = retrieve_role_memories(
            self.conn,
            {"signal_stats": [{"signal_key": "OKX|perp_funding_basis"}]},
            "cross_market_researcher",
            {"agent_memory": {"retrieval_limit_per_agent": 1}},
            cycle_id="descriptive-signal",
        )[0]

        self.assertIn("57 valid labels", memory["summary"])
        self.assertIn("average net outcome was +31.10 bps", memory["summary"])
        self.assertIn("57.1% of labels were profitable", memory["summary"])
        self.assertIn("instrument, session, liquidity, and regime slices", memory["summary"])
        self.assertNotIn('{"avg_pnl_bps"', memory["summary"])

    def test_strategy_and_code_memories_explain_evidence_and_result(self) -> None:
        strategy_payload = {
            "strategy_lab_id": "lab-regional-reversal",
            "status": "retired_bad_evidence",
            "hypothesis": "Buy verified regional dislocations only when local FX and depth agree.",
            "strategy_logic": {
                "type": "candidate_filter",
                "allowed_regions": ["Africa", "LATAM"],
                "min_quality_score": 75,
            },
            "data_requirements": {"requires_verified_depth": True, "requires_fresh_fx": True},
            "risk_gates": {"max_spread_bps": 12, "paper_only": True},
            "evaluation": {
                "active_hours": 52,
                "outcomes": {
                    "trade_count": 34,
                    "valid_count": 31,
                    "metrics": {
                        "count": 31,
                        "avg_pnl_bps": -12.5,
                        "win_rate": 0.419,
                        "worst_decile_pnl_bps": -61.2,
                    },
                    "by_venue": {
                        "LUNO": {"count": 20, "avg_pnl_bps": -18.0, "win_rate": 0.35},
                        "VALR": {"count": 11, "avg_pnl_bps": -2.5, "win_rate": 0.545},
                    },
                    "route_status_counts": {"standard": 22, "conditional": 12},
                },
            },
        }
        upsert_memory_fact(
            self.conn,
            "strategy_lab_evaluation",
            "lab-regional-reversal",
            "retired_bad_evidence",
            json.dumps(strategy_payload),
            0.94,
            "strategy_lab",
            strategy_payload,
            namespace="strategies",
        )
        code_payload = {
            "proposal_id": "proposal-42",
            "title": "Wire regional depth quality into paper scoring",
            "source_agent": "build_planner",
            "category": "paper_scoring_logic",
            "status": "promoted",
            "changed_files": ["src/frontier_data_quality.py", "tests/test_frontier_data_quality.py"],
            "candidate_commit": "abc123",
            "tests": {"focused": {"passed": True}, "full_regression": {"passed": True}},
            "promotion_reason": "Candidate passed sandbox gates.",
        }
        upsert_memory_fact(
            self.conn,
            "code_evolution_outcome",
            "proposal-42",
            "promoted",
            json.dumps(code_payload),
            0.98,
            "code_evolution",
            code_payload,
            namespace="code",
        )

        rows = {
            row["namespace"]: row["summary"]
            for row in self.conn.execute(
                "select namespace, summary from temporal_memories where subject in (?, ?)",
                ("lab-regional-reversal", "proposal-42"),
            )
        }
        self.assertIn("recorded this hypothesis", rows["strategies"])
        self.assertIn("executable strategy contract", rows["strategies"])
        self.assertIn("min quality score: 75", rows["strategies"])
        self.assertIn("requires verified depth: True", rows["strategies"])
        self.assertIn("31 valid reliable outcomes", rows["strategies"])
        self.assertIn("LUNO: -18.00 bps across 20 labels", rows["strategies"])
        self.assertIn("Wire regional depth quality", rows["code"])
        self.assertIn("src/frontier_data_quality.py", rows["code"])
        self.assertIn("focused, full_regression", rows["code"])

    def test_recommendation_and_route_memories_are_actionable_text(self) -> None:
        recommendation = {
            "agent_name": "market_scout",
            "action": "request_market_adapter",
            "title": "Add public price discovery for a regional exchange",
            "status": "accepted",
            "market_key": "regional_equities",
            "rationale": "The exchange publishes no-key quotes and the current map lacks this country.",
            "proposed_change": "Create a read-only adapter and feed normalized observations into paper review.",
            "downstream_code": [],
        }
        upsert_memory_fact(
            self.conn,
            "recommendation_outcome",
            "recommendation-7",
            "accepted",
            json.dumps(recommendation),
            0.9,
            "llm_recommendation_pipeline",
            recommendation,
            namespace="recommendations",
        )
        route = {
            "by_route_status": {"standard": 42, "conditional": 29, "blocked": 179},
            "by_missing_requirement": {"spot_borrow": 27, "prediction_markets_account": 2},
            "top_manual_actions": [
                {
                    "requirement_id": "spot_borrow",
                    "count": 27,
                    "suggested_action": "Confirm borrow support or retain the paper proxy.",
                }
            ],
        }
        upsert_memory_fact(
            self.conn,
            "route_resolver",
            "execution_routes",
            "has_route_summary",
            json.dumps(route["by_route_status"]),
            0.9,
            "route_resolver",
            route,
            namespace="routes",
        )

        recommendation_text = self.conn.execute(
            "select summary from temporal_memories where subject='recommendation-7'"
        ).fetchone()["summary"]
        route_text = self.conn.execute(
            "select summary from temporal_memories where subject='execution_routes'"
        ).fetchone()["summary"]
        self.assertIn("The exchange publishes no-key quotes", recommendation_text)
        self.assertIn("Create a read-only adapter", recommendation_text)
        self.assertIn("No downstream code proposal has been recorded yet", recommendation_text)
        self.assertIn("spot borrow: 27", route_text)
        self.assertIn("feasibility, not strategy profitability", route_text)

    def test_rich_prose_backfill_updates_old_active_rows_once(self) -> None:
        evidence = {
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "direction": "funding_capture_short_perp",
            "valid_labels": 40,
            "avg_pnl_bps": 18.2,
            "win_rate": 0.55,
            "worst_decile_bps": -24.0,
            "best_bps": 90.0,
            "worst_bps": -50.0,
        }
        result = upsert_memory_fact(
            self.conn,
            "signal_performance",
            "OKX|perp_funding_basis|funding_capture_short_perp|standard",
            "has_reliable_60m_outcomes",
            json.dumps(evidence),
            0.95,
            "paper_outcome_engine",
            evidence,
            namespace="outcomes",
            outcome=evidence,
        )
        self.conn.execute(
            "update temporal_memories set summary='signal_performance: old terse text' where memory_id=?",
            (result["memory_id"],),
        )
        self.conn.commit()

        first = refresh_memory_prose(self.conn)
        second = refresh_memory_prose(self.conn)
        row = self.conn.execute(
            "select summary from temporal_memories where memory_id=?", (result["memory_id"],)
        ).fetchone()

        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["status"], "already_complete")
        self.assertIn("40 valid labels", row["summary"])
        self.assertIn("average net outcome was +18.20 bps", row["summary"])


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

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = pathlib.Path(tmp) / "checkpoints.sqlite"
            with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_agent):
                llm_swarm_runner.run_langgraph_if_available(
                    packet,
                    role_memory,
                    {
                        "agent_memory": {
                            "checkpoint_enabled": True,
                            "checkpoint_path": str(checkpoint_path),
                            "checkpoint_retention_cycles": 8,
                            "checkpoint_max_storage_mb": 8,
                        }
                    },
                    "cycle-test",
                )
            checkpoint_status = llm_swarm_runner.LAST_SWARM_STATE["checkpoint"]["status"]
            channels: set[str] = set()
            if checkpoint_status == "saved":
                checkpoint_conn = sqlite3.connect(checkpoint_path)
                try:
                    channels = {
                        row[0]
                        for row in checkpoint_conn.execute("select distinct channel from writes")
                    }
                finally:
                    checkpoint_conn.close()

        self.assertEqual(set(observed), {agent["name"] for agent in llm_swarm_runner.AGENTS})
        for agent in llm_swarm_runner.AGENTS:
            self.assertEqual(observed[agent["name"]], f"mem-{agent['name']}")
        if checkpoint_status == "saved":
            self.assertNotIn("packet", channels)
            self.assertNotIn("role_memory", channels)
            self.assertNotIn("memory", channels)
            self.assertIn("checkpoint_context", channels)
        self.assertNotIn("packet", llm_swarm_runner.LAST_SWARM_STATE)
        self.assertNotIn("role_memory", llm_swarm_runner.LAST_SWARM_STATE)
        context = llm_swarm_runner.LAST_SWARM_STATE["checkpoint"]["runtime_context"]
        self.assertEqual(context["runtime_context_mode"], "reference_only")
        self.assertGreater(context["packet_bytes"], 0)
        self.assertEqual(context["role_memory_counts"]["market_scout"], 1)
        self.assertIn(
            llm_swarm_runner.LAST_SWARM_STATE["checkpoint"]["status"],
            {"saved", "package_missing"},
        )

    def test_checkpoint_pruning_enforces_cycle_and_payload_limits(self) -> None:
        class FakeSaver:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self.conn = conn

            def setup(self) -> None:
                return None

            def delete_thread(self, thread_id: str) -> None:
                self.conn.execute("delete from checkpoints where thread_id = ?", (thread_id,))
                self.conn.execute("delete from writes where thread_id = ?", (thread_id,))

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "create table checkpoints (thread_id text, checkpoint blob, metadata blob)"
            )
            conn.execute("create table writes (thread_id text, value blob)")
            for index in range(8):
                thread_id = f"swarm:{index}"
                conn.execute(
                    "insert into checkpoints(thread_id, checkpoint, metadata) values (?, ?, ?)",
                    (thread_id, b"x" * 2048, b"m" * 128),
                )
                conn.execute(
                    "insert into writes(thread_id, value) values (?, ?)",
                    (thread_id, b"w" * 512),
                )
            conn.commit()

            removed = llm_swarm_runner._prune_checkpoint_threads(
                FakeSaver(conn),
                conn,
                retain=8,
                max_storage_mb=0.006,
            )
            retained = [
                row[0]
                for row in conn.execute(
                    "select distinct thread_id from checkpoints order by rowid"
                )
            ]
        finally:
            conn.close()

        self.assertEqual(removed, 6)
        self.assertEqual(retained, ["swarm:6", "swarm:7"])


if __name__ == "__main__":
    unittest.main()
