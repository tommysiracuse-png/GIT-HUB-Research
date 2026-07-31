from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest
import json


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import evolution_worker
import radar_loop
import code_evolution
import self_improvement
import storage


class EvolutionWorkerSeparationTests(unittest.TestCase):
    def test_radar_defaults_skip_slow_llm_and_builder_work(self) -> None:
        policy = radar_loop._auxiliary_runtime_policy(
            {
                "llm_swarm": {"enabled": True, "auto_run": True},
                "autonomous_builder": {"enabled": True, "auto_run": True},
                "evolution_worker": {"enabled": True},
            }
        )

        self.assertFalse(policy["llm_swarm_in_radar"])
        self.assertFalse(policy["autonomous_builder_in_radar"])
        self.assertTrue(policy["evolution_worker_expected"])

    def test_worker_settings_enable_code_changes_without_mutating_input(self) -> None:
        settings = {"self_improvement": {"process_code_changes_in_radar_loop": False}}

        worker_settings = evolution_worker._worker_settings(settings)

        self.assertFalse(settings["self_improvement"]["process_code_changes_in_radar_loop"])
        self.assertTrue(worker_settings["self_improvement"]["process_code_changes_in_radar_loop"])

    def test_recommendation_fetch_can_exclude_code_changes_for_radar(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            storage.add_llm_recommendation(
                conn,
                "rec-code",
                "propose_code_change",
                "Code",
                "Change code",
                {"action": "propose_code_change", "priority": 99},
            )
            storage.add_llm_recommendation(
                conn,
                "rec-task",
                "propose_build_task",
                "Task",
                "Build task",
                {"action": "propose_build_task", "priority": 80},
            )

            radar_items = storage.llm_recommendations_for_auto_execution(
                conn,
                limit=10,
                include_code_changes=False,
            )
            worker_items = storage.llm_recommendations_for_auto_execution(
                conn,
                limit=10,
                include_code_changes=True,
            )
        finally:
            conn.close()

        self.assertEqual([item["recommendation_id"] for item in radar_items], ["rec-task"])
        self.assertEqual(
            [item["recommendation_id"] for item in worker_items],
            ["rec-code", "rec-task"],
        )

    def test_route_probe_task_serializes_dict_rationale(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            created = storage.add_route_probe_task(
                conn,
                "rec-route",
                "execution_routes",
                "conditional",
                90,
                "read_only_capability_probe",
                {"behavior": "validate paper route", "paper_only": True},
                {"blockers": {"spot_borrow": 12}},
            )
            row = conn.execute("select rationale, evidence_json from route_probe_tasks").fetchone()
        finally:
            conn.close()

        self.assertTrue(created)
        self.assertIn("validate paper route", row["rationale"])
        self.assertEqual({"blockers": {"spot_borrow": 12}}, json.loads(row["evidence_json"]))

    def test_implementation_like_build_task_routes_to_code_change(self) -> None:
        payload = {
            "action": "propose_build_task",
            "priority": 93,
            "title": "Add context-aware paper scoring for route and liquidity divergence",
            "proposed_change": {
                "expected_behavior": "Promote standard-feasibility carry and suppress weak conditional routes.",
                "paper_scope": "Use only for paper recommendation scoring.",
            },
            "evidence": {"source": "llm_swarm"},
        }

        self.assertEqual("code_change", self_improvement.classify_recommendation(payload))
        normalized = self_improvement._normalize_code_change_recommendation(
            {"recommendation_id": "rec-build", "title": payload["title"], "payload": payload}
        )

        self.assertEqual("propose_code_change", normalized["payload"]["action"])
        self.assertEqual("paper_scoring_logic", normalized["payload"]["change_category"])
        self.assertNotIn("expected_files", normalized["payload"])
        preflight = code_evolution.preflight_proposal(normalized["payload"], {"code_evolution": {}})
        self.assertIn("src/strategy_reliability.py", preflight["target_files"])

    def test_manual_account_route_task_stays_route_resolver(self) -> None:
        payload = {
            "action": "propose_build_task",
            "priority": 90,
            "title": "Review prediction market account and jurisdiction eligibility",
            "proposed_change": "Human decision needed for account setup and jurisdiction eligibility.",
        }

        self.assertEqual("route_resolver", self_improvement.classify_recommendation(payload))

    def test_unseen_global_market_surface_maps_by_intent_not_name(self) -> None:
        payload = {
            "action": "request_market_adapter",
            "priority": 88,
            "title": "Add Nairobi Coffee Exchange auction feed",
            "proposed_change": (
                "Build a public no-key parser for the official auction price feed, "
                "daily settlement data, and instrument list. Ingest observations into "
                "paper research so the global discovery worker can rank the market surface."
            ),
            "evidence": {"surface_type_raw": "agricultural auction market", "data_access_type": "public_no_key"},
        }

        self.assertEqual("code_change", self_improvement.classify_recommendation(payload))
        normalized = self_improvement._normalize_code_change_recommendation(
            {"recommendation_id": "rec-nairobi", "title": payload["title"], "payload": payload}
        )

        self.assertEqual("public_data_adapter", normalized["payload"]["change_category"])
        self.assertNotIn("expected_files", normalized["payload"])
        preflight = code_evolution.preflight_proposal(normalized["payload"], {"code_evolution": {}})
        self.assertIn("src/adapter_runtime.py", preflight["target_files"])
        self.assertIn("src/adapter_capabilities.py", preflight["target_files"])
        self.assertNotIn("src/research_worker.py", preflight["target_files"])
        self.assertNotIn("src/frontier_crypto_adapter.py", preflight["target_files"])

    def test_unknown_crypto_exchange_still_maps_to_frontier_adapter_without_name_allowlist(self) -> None:
        payload = {
            "action": "request_market_adapter",
            "priority": 88,
            "title": "Add MeridianX crypto order book feed",
            "proposed_change": (
                "Create a public REST parser for spot ticker and order book depth, normalize "
                "bid ask quotes, and wire the observations into the frontier scanner."
            ),
        }

        normalized = self_improvement._normalize_code_change_recommendation(
            {"recommendation_id": "rec-meridianx", "title": payload["title"], "payload": payload}
        )

        self.assertEqual("public_data_adapter", normalized["payload"]["change_category"])
        self.assertNotIn("expected_files", normalized["payload"])
        preflight = code_evolution.preflight_proposal(normalized["payload"], {"code_evolution": {}})
        self.assertIn("src/frontier_crypto_adapter.py", preflight["target_files"])


if __name__ == "__main__":
    unittest.main()
