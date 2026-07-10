from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import evolution_worker
import radar_loop
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


if __name__ == "__main__":
    unittest.main()
