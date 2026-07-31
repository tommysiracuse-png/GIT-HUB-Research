import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement


class SelfImprovementLoopSmokeTests(unittest.TestCase):
    def test_module_imports(self) -> None:
        import tests.test_self_improvement_loop  # noqa: F401

    def test_radar_lane_skips_inferred_code_changes_without_consuming_them(self) -> None:
        code_change = {
            "recommendation_id": "build-1",
            "title": "Wire a public adapter",
            "payload": {
                "action": "propose_build_task",
                "priority": 95,
                "code_change": {
                    "change_category": "public_data_adapter",
                    "expected_files": ["src/research_worker.py"],
                },
            },
        }
        adapter = {
            "recommendation_id": "adapter-1",
            "title": "Research a public venue",
            "payload": {
                "action": "request_market_adapter",
                "priority": 80,
                "market_key": "TEST",
            },
        }

        selected = self_improvement._select_recommendations_for_lane(
            [code_change, adapter],
            1,
            include_code_changes=False,
        )

        self.assertEqual([(item[0]["recommendation_id"], item[1]) for item in selected], [("adapter-1", "market_adapter")])

    def test_evolution_lane_keeps_inferred_code_changes(self) -> None:
        recommendation = {
            "recommendation_id": "build-1",
            "payload": {
                "action": "propose_build_task",
                "code_change": {
                    "change_category": "public_data_adapter",
                    "expected_files": ["src/research_worker.py"],
                },
            },
        }

        selected = self_improvement._select_recommendations_for_lane(
            [recommendation],
            1,
            include_code_changes=True,
        )

        self.assertEqual(selected[0][1], "code_change")


if __name__ == "__main__":
    unittest.main()
