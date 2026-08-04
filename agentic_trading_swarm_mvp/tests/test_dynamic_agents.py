from __future__ import annotations

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

import dynamic_agents
import llm_bridge
import llm_swarm_runner
import storage


def agent_spec(**overrides):
    payload = {
        "name": "frontier_depth_specialist",
        "objective": "Diagnose recurring frontier depth failures and propose durable repairs.",
        "triggers": {"any_packet_paths": ["frontier_crypto_venues"], "cooldown_minutes": 0},
        "evidence_inputs": ["frontier_crypto_venues", "signal_stats"],
        "memory_policy": {
            "namespaces": ["markets", "outcomes", "code"],
            "keywords": ["frontier depth parser quality"],
            "retrieval_limit": 17,
        },
        "model_tier": "standard",
        "allowed_actions": ["propose_code_change", "spawn_agent"],
        "success_measure": {"kept_code_changes": 1},
    }
    payload.update(overrides)
    return payload


class DynamicAgentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_exact_duplicate_merges_and_preserves_all_parents(self):
        first = dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(parent_agent_id="market_scout"),
            source_recommendation_id="rec-1",
        )
        second = dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(name="same_work_different_label", parent_agent_id="agent_parent_2"),
            source_recommendation_id="rec-2",
        )

        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(second["status"], "merged_exact_duplicate")
        row = self.conn.execute("select parent_ids_json, merged_count from agent_specs").fetchone()
        self.assertEqual(set(json.loads(row["parent_ids_json"])), {"market_scout", "agent_parent_2"})
        self.assertEqual(row["merged_count"], 1)
        parents = {row[0] for row in self.conn.execute("select parent_agent_id from agent_lineage")}
        self.assertEqual(parents, {"market_scout", "agent_parent_2"})

    def test_bootstrap_creates_first_generation_once_through_normal_registry(self):
        settings = {"dynamic_agents": {"enabled": True, "bootstrap_seed_agents": True}}

        first = dynamic_agents.bootstrap_seed_agents(self.conn, settings)
        second = dynamic_agents.bootstrap_seed_agents(self.conn, settings)

        self.assertEqual(first["created"], 4)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 4)
        self.assertEqual(self.conn.execute("select count(*) from agent_specs").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("select coalesce(sum(merged_count),0) from agent_specs").fetchone()[0], 0)
        parents = {row[0] for row in self.conn.execute("select parent_agent_id from agent_lineage")}
        self.assertEqual(
            parents,
            {"market_scout", "strategy_lab", "build_planner", "cross_market_researcher"},
        )

    def test_bootstrap_agents_trigger_on_relevant_live_packet_surfaces(self):
        cycle = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn,
            {
                "expansion_map": {"markets": 2},
                "frontier_crypto_venues": {"observations": 10},
                "strategy_lab": {"active": 3},
                "horizon_outcomes": [{"horizon": "60m"}],
                "code_evolution": {"implementation_paused": 1},
            },
            {"dynamic_agents": {"enabled": True, "bootstrap_seed_agents": True}},
            "swarm:bootstrap",
        )

        self.assertEqual(cycle["bootstrap"]["created"], 4)
        self.assertEqual(cycle["active_count"], 4)
        self.assertEqual(cycle["matched_count"], 4)
        self.assertEqual({item["display_name"] for item in cycle["matched_agents"]}, {
            "global_market_expansion_specialist",
            "novel_strategy_discovery_specialist",
            "code_evolution_reliability_specialist",
            "market_data_quality_specialist",
        })

    def test_bootstrap_can_be_disabled(self):
        report = dynamic_agents.bootstrap_seed_agents(
            self.conn,
            {"dynamic_agents": {"bootstrap_seed_agents": False}},
        )

        self.assertEqual(report["status"], "disabled")
        self.assertEqual(self.conn.execute("select count(*) from agent_specs").fetchone()[0], 0)

    def test_child_generation_and_next_cycle_activation(self):
        parent = dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(name="parent", triggers={"always": True}),
        )
        child = dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(
                name="child",
                objective="Study route failures produced by the parent specialist.",
                parent_agent_id=parent["agent_id"],
                triggers={"any_terms": ["spot_borrow"]},
            ),
        )

        cycle = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn,
            {"route_intelligence": {"blocker": "spot_borrow"}},
            {"dynamic_agents": {"enabled": True, "adaptive_concurrency": 8}},
            "swarm:next",
        )

        ids = {item["dynamic_agent_id"] for item in cycle["matched_agents"]}
        self.assertIn(parent["agent_id"], ids)
        self.assertIn(child["agent_id"], ids)
        child_row = self.conn.execute("select generation, activation_cycle_id from agent_specs where agent_id=?", (child["agent_id"],)).fetchone()
        self.assertEqual(child_row["generation"], 2)
        self.assertEqual(child_row["activation_cycle_id"], "swarm:next")

    def test_trigger_mismatch_keeps_agent_dormant(self):
        dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(triggers={"all_packet_paths": ["missing.path"]}),
        )
        cycle = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn,
            {"summary": {}},
            {"dynamic_agents": {"enabled": True, "adaptive_concurrency": 8}},
            "swarm:dormant",
        )
        self.assertEqual(cycle["matched_count"], 0)
        self.assertEqual(cycle["dormant_count"], 1)
        self.assertEqual(cycle["evaluated"][0]["reason"], "trigger_not_matched")

    def test_term_trigger_runs_again_only_when_matching_evidence_changes(self):
        created = dynamic_agents.register_agent_spec(
            self.conn,
            agent_spec(triggers={"any_terms": ["spot_borrow"], "cooldown_minutes": 0}),
        )
        settings = {"dynamic_agents": {"enabled": True, "evidence_delta_triggers": True}}
        first = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn, {"routes": [{"blocker": "spot_borrow", "count": 2}]}, settings, "cycle-1"
        )
        self.conn.execute("update agent_specs set last_run_at=? where agent_id=?", (storage.utc_now(), created["agent_id"]))
        self.conn.commit()
        unchanged = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn, {"routes": [{"blocker": "spot_borrow", "count": 2}]}, settings, "cycle-2"
        )
        changed = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn, {"routes": [{"blocker": "spot_borrow", "count": 7}]}, settings, "cycle-3"
        )

        self.assertEqual(1, first["matched_count"])
        self.assertEqual("evidence_unchanged", unchanged["evaluated"][0]["reason"])
        self.assertEqual(1, changed["matched_count"])

    def test_agent_architect_discovers_arbitrary_recurring_recommendation_cluster(self):
        for index in range(3):
            storage.add_llm_recommendation(
                self.conn,
                f"rec-adapter-{index}",
                "request_market_adapter",
                f"Add public auction venue parser {index}",
                "A recurring public auction market has priceable data but no runtime adapter.",
                {"market_key": "public_auction_surface", "priority": 86},
            )
        settings = {
            "dynamic_agents": {
                "agent_architect_enabled": True,
                "spawn_cluster_min_count": 3,
                "spawn_objective_overlap_threshold": 0.82,
            }
        }
        first = dynamic_agents.prepare_agent_architect(self.conn, {"market_discovery": {}}, settings, "cycle-a")
        second = dynamic_agents.prepare_agent_architect(self.conn, {"market_discovery": {}}, settings, "cycle-b")
        recommendation = dynamic_agents.architect_recommendation({"agent_architect": second})
        result = dynamic_agents.ingest_spawn_agent_recommendation(
            self.conn, recommendation, recommendation_id="spawn-rec-1"
        )

        self.assertIsNone(first["spawn_candidate"])
        self.assertEqual("spawn_agent", recommendation["action"])
        self.assertEqual("created", result["status"])
        self.assertEqual(1, self.conn.execute("select count(*) from agent_specs").fetchone()[0])
        self.assertEqual("spawned", self.conn.execute("select status from agent_spawn_candidates").fetchone()[0])

    def test_role_specific_memory_policy_is_forwarded(self):
        created = dynamic_agents.register_agent_spec(self.conn, agent_spec(triggers={"always": True}))
        cycle = dynamic_agents.prepare_dynamic_agent_cycle(
            self.conn,
            {},
            {"dynamic_agents": {"enabled": True}},
            "swarm:memory",
        )
        captured = {}

        def fake_retrieve(_conn, _packet, name, _settings, *, cycle_id, policy_override=None):
            captured.update({"name": name, "cycle_id": cycle_id, "policy": policy_override})
            return [{"memory_id": "m1"}]

        with mock.patch("temporal_memory.retrieve_role_memories", side_effect=fake_retrieve):
            contexts = dynamic_agents.build_dynamic_memory_contexts(
                self.conn, {}, cycle["matched_agents"], {}, "swarm:memory"
            )

        runtime_name = cycle["matched_agents"][0]["name"]
        self.assertEqual(created["agent_id"], cycle["matched_agents"][0]["dynamic_agent_id"])
        self.assertEqual(contexts[runtime_name][0]["memory_id"], "m1")
        self.assertEqual(captured["policy"]["retrieval_limit"], 17)
        self.assertEqual(captured["cycle_id"], "swarm:memory")

    def test_adaptive_concurrency_defaults_to_eight_and_backs_off_on_quota(self):
        normal = dynamic_agents.adaptive_concurrency(self.conn, {"dynamic_agents": {}})
        self.assertEqual(normal["effective"], 8)
        now = storage.utc_now()
        for index in range(2):
            self.conn.execute(
                """insert into agent_runs(
                run_id, agent_id, cycle_id, started_at, status, trigger_match_json,
                memory_ids_json, model_json, recommendation_json, estimated_cost_usd
                ) values(?,?,?,?,?,?,?,?,?,0)""",
                (f"r{index}", f"a{index}", f"c{index}", now, "model_unavailable", "{}", "[]", '{"status":"quota_429"}', "{}"),
            )
        self.conn.commit()
        backed_off = dynamic_agents.adaptive_concurrency(self.conn, {"dynamic_agents": {"adaptive_concurrency": 8}})
        self.assertEqual(backed_off["effective"], 4)
        self.assertIn("recent_model_quota_pressure", backed_off["reasons"])

    def test_run_record_links_recommendation_to_code_and_strategy_outcomes(self):
        created = dynamic_agents.register_agent_spec(self.conn, agent_spec(triggers={"always": True}))
        cycle = dynamic_agents.prepare_dynamic_agent_cycle(self.conn, {}, {"dynamic_agents": {}}, "swarm:links")
        agent = cycle["matched_agents"][0]
        rec = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Repair depth parser",
            "rationale": "Measured parser failures.",
            "evidence": {},
            "proposed_change": "Repair parser.",
            "agent_name": agent["name"],
            "model": {"status": "model_call:test", "tier": "standard", "estimated_cost_usd": 0.02},
        }
        rec = dynamic_agents.decorate_dynamic_recommendation(agent, rec, "swarm:links")
        state = {
            "agent_outputs": [{
                "agent_name": agent["name"], "dynamic_agent_id": created["agent_id"],
                "accepted": True, "recommendation": rec, "model": rec["model"], "memory_ids": ["m1"],
            }],
            "graph_trace": [{"node": agent["name"], "dynamic_agent_id": created["agent_id"], "elapsed_ms": 20}],
        }
        dynamic_agents.record_dynamic_agent_runs(self.conn, state, cycle, "swarm:links")
        recommendation_id = self.conn.execute("select recommendation_id from agent_runs").fetchone()[0]
        now = storage.utc_now()
        self.conn.execute(
            """insert into code_evolution_proposals(
            proposal_id, created_at, updated_at, source_recommendation_id, title, category,
            priority, status, payload_json, evidence_json
            ) values('p1',?,?,?,?,?,90,'promoted','{}','{}')""",
            (now, now, recommendation_id, "Repair", "parser_improvement"),
        )
        self.conn.execute(
            """insert into strategy_lab_experiments(
            strategy_lab_id, version, status, hypothesis, strategy_logic_json,
            data_requirements_json, risk_gates_json, promotion_rules_json,
            source_recommendation_id, created_at, updated_at
            ) values('lab1',1,'active_testing','test','{}','{}','{}','{}',?,?,?)""",
            (recommendation_id, now, now),
        )
        self.conn.execute(
            """insert into paper_trades(
            opened_at, closed_at, venue, inst_id, direction, trade_type, signal_key,
            base_score, learned_score, entry, exit, pnl_bps, status, thesis,
            candidate_json, review_json, strategy_lab_id
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, now, "TEST", "ABC", "long", "lab", "lab1", 1, 1, 100, 101, 100, "closed", "test", "{}", "{}", "lab1"),
        )
        self.conn.commit()

        summary = dynamic_agents.dynamic_agent_summary(self.conn)
        downstream = summary["latest_runs"][0]["downstream"]
        self.assertEqual(downstream["code_proposals"][0]["proposal_id"], "p1")
        self.assertEqual(downstream["strategy_experiments"][0]["paper_outcomes"]["closed"], 1)
        linked = self.conn.execute("select code_proposal_id, strategy_lab_id from agent_runs").fetchone()
        self.assertEqual(tuple(linked), ("p1", "lab1"))


class DynamicAgentBridgeAndGraphTests(unittest.TestCase):
    def test_spawn_agent_inbox_ingestion_persists_and_marks_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            inbox = pathlib.Path(tmp) / "inbox.jsonl"
            processed = pathlib.Path(tmp) / "processed.jsonl"
            payload = {
                "action": "spawn_agent", "priority": 88, "title": "Create depth specialist",
                "rationale": "Recurring depth failures need durable ownership.",
                "evidence": {"failures": 12}, "proposed_change": "Create specialist.",
                "agent_name": "build_planner", "agent_spec": agent_spec(),
            }
            inbox.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with mock.patch.object(llm_bridge, "INBOX", inbox), mock.patch.object(llm_bridge, "PROCESSED", processed):
                accepted = llm_bridge.ingest_llm_recommendations(
                    conn,
                    {"llm_bridge": {"enabled": True, "ingest_recommendations": True, "max_recommendations_per_loop": 20, "allowed_actions": ["spawn_agent"]}},
                )
            self.assertEqual(accepted[0]["action"], "spawn_agent")
            self.assertEqual(conn.execute("select count(*) from agent_specs").fetchone()[0], 1)
            self.assertEqual(conn.execute("select status from llm_recommendations").fetchone()[0], "agent_spawned")
            conn.close()

    def test_dynamic_agent_is_loaded_as_langgraph_node_and_visible_to_planner(self):
        dynamic = dynamic_agents.runtime_agent({
            "agent_id": "agent_test", "name": "test_specialist", "objective": "Analyze frontier evidence.",
            "allowed_actions": ["propose_diagnostic_hypothesis"], "model_tier": "fast",
            "parent_ids": ["market_scout"], "generation": 1,
            "memory_policy": {}, "evidence_inputs": [], "success_measure": {},
        })
        seen_by_dynamic = []
        seen_by_planner = []

        def fake_run(agent, packet, _memory):
            if agent.get("dynamic_agent_id"):
                seen_by_dynamic.extend(row.get("agent_name") for row in packet.get("current_cycle_agent_outputs", []))
            if agent["name"] == "build_planner":
                seen_by_planner.extend(row.get("agent_name") for row in packet.get("current_cycle_agent_outputs", []))
            return {
                "action": "propose_diagnostic_hypothesis", "priority": 60,
                "title": agent["name"], "rationale": "test", "market_key": agent["name"],
                "evidence": {}, "proposed_change": "test", "agent_name": agent["name"],
                "model": {"status": "model_call:test", "tier": "fast", "estimated_cost_usd": 0},
            }

        packet = {"allowed_recommendation_actions": ["propose_diagnostic_hypothesis"]}
        cycle = {"matched_agents": [dynamic], "concurrency": {"effective": 8}}
        with mock.patch.object(llm_swarm_runner, "run_agent", side_effect=fake_run):
            llm_swarm_runner.run_langgraph_if_available(
                packet, {}, cycle_id="swarm:graph", dynamic_agents=[dynamic], dynamic_cycle=cycle
            )

        self.assertIn("market_scout", seen_by_dynamic)
        self.assertIn("cross_market_researcher", seen_by_dynamic)
        self.assertIn(dynamic["name"], seen_by_planner)
        trace = llm_swarm_runner.LAST_SWARM_STATE["graph_trace"]
        self.assertTrue(any(row.get("dynamic_agent_id") == "agent_test" for row in trace))

    def test_dynamic_agent_cannot_emit_action_outside_its_contract(self):
        dynamic = dynamic_agents.runtime_agent({
            "agent_id": "agent_limited", "name": "limited", "objective": "Study routes only.",
            "allowed_actions": ["propose_hunter_directive"], "model_tier": "fast",
            "parent_ids": [], "generation": 1, "memory_policy": {}, "evidence_inputs": [], "success_measure": {},
        })
        rec = llm_swarm_runner.parse_recommendation(
            json.dumps({
                "action": "spawn_agent", "priority": 80, "title": "Out of scope",
                "rationale": "test", "market_key": "test", "evidence": {},
                "proposed_change": "test", "agent_spec": agent_spec(),
            }),
            dynamic,
            {"allowed_recommendation_actions": ["spawn_agent", "propose_hunter_directive"]},
        )
        self.assertTrue(rec["_rejected"])
        self.assertEqual(rec["terminal_failure_reason"], "action_not_allowed_for_dynamic_agent")


if __name__ == "__main__":
    unittest.main()
