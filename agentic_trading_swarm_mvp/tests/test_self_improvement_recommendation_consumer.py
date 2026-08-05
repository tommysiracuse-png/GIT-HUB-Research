import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import (  # noqa: E402
    RECOMMENDATION_REJECTION_TTL_SECONDS,
    _CONSUMER_REJECTION_CACHE,
    _execute_strategy_lab_experiment,
    _normalize_code_change_recommendation,
)
from storage import add_llm_recommendation, init_db  # noqa: E402


class SelfImprovementRecommendationConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        _CONSUMER_REJECTION_CACHE.clear()

    def test_normalize_adds_consumer_validation_for_clean_payload(self) -> None:
        rec = {
            "agent_name": "builder",
            "payload": {
                "action": "propose_build_task",
                "title": "Add audit trace field",
                "code_change": {
                    "change_category": "runtime_pipeline_integration",
                    "expected_files": ["src/self_improvement.py", "tests/test_smart_failure_filters.py"],
                },
            },
        }

        normalized = _normalize_code_change_recommendation(rec)
        payload = normalized["payload"]
        audit = payload["consumer_validation"]

        self.assertEqual(payload["action"], "propose_code_change")
        self.assertEqual(payload["expected_files"], ["src/self_improvement.py", "tests/test_smart_failure_filters.py"])
        self.assertEqual(audit["validation_stage"], "consumer")
        self.assertEqual(audit["source_agent"], "builder")
        self.assertIsNone(audit["rejection_reason"])
        self.assertIsNone(audit["suppressed_until"])
        self.assertEqual(payload["code_change"]["target_selection_mode"], "explicit")

    def test_normalize_filters_disallowed_expected_files_and_sets_suppression(self) -> None:
        rec = {
            "payload": {
                "action": "propose_code_change",
                "code_change": {
                    "change_category": "runtime_pipeline_integration",
                    "expected_files": [
                        "src/self_improvement.py",
                        "../secrets.env",
                        "C:/tmp/outside.py",
                        "docs/guard.md",
                    ],
                },
            }
        }

        normalized = _normalize_code_change_recommendation(rec)
        payload = normalized["payload"]
        audit = payload["consumer_validation"]

        self.assertEqual(payload["expected_files"], ["src/self_improvement.py", "docs/guard.md"])
        self.assertEqual(audit["rejection_reason"], "disallowed_expected_files")
        self.assertEqual(audit["invalid_expected_files"], ["../secrets.env", "C:/tmp/outside.py"])
        self.assertIsNotNone(audit["suppressed_until"])
        self.assertEqual(payload["code_change"]["target_selection_mode"], "explicit")

    def test_repeat_invalid_payload_reuses_fingerprint_suppression_window(self) -> None:
        rec = {
            "agent_name": "builder",
            "payload": {
                "action": "propose_code_change",
                "code_change": {
                    "change_category": "runtime_pipeline_integration",
                    "expected_files": ["../outside.py"],
                },
            },
        }

        first = _normalize_code_change_recommendation(rec)
        second = _normalize_code_change_recommendation(rec)

        first_audit = first["payload"]["consumer_validation"]
        second_audit = second["payload"]["consumer_validation"]
        self.assertEqual(first_audit["proposal_fingerprint"], second_audit["proposal_fingerprint"])
        self.assertEqual(first_audit["suppressed_until"], second_audit["suppressed_until"])
        self.assertGreater(RECOMMENDATION_REJECTION_TTL_SECONDS, 0)

    def test_complete_strategy_contract_materializes_without_owner_queue(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        contract = {
            "strategy_lab_id": "direct_cross_sectional_strength_v1",
            "version": 1,
            "experiment_type": "market_strategy",
            "hypothesis": "Cross-sectional strength persists after costs.",
            "source_surface": "global_proxy_momentum",
            "permitted_target_surface": ["global_proxy_momentum"],
            "strategy_logic": {
                "type": "candidate_filter",
                "trade_types": ["global_proxy_momentum"],
                "directions": ["long_proxy"],
            },
            "data_requirements": {"paper_only": True},
            "risk_gates": {},
            "promotion_rules": {},
        }
        payload = {
            "action": "propose_strategy_lab_experiment",
            "priority": 90,
            "title": "Direct cross-sectional experiment",
            "rationale": contract["hypothesis"],
            "strategy_lab_experiment": contract,
        }
        add_llm_recommendation(
            conn,
            "rec-direct-contract",
            payload["action"],
            payload["title"],
            payload["rationale"],
            payload,
        )
        rec = {
            "recommendation_id": "rec-direct-contract",
            "title": payload["title"],
            "rationale": payload["rationale"],
            "payload": payload,
        }

        artifacts = _execute_strategy_lab_experiment(conn, rec, {})

        self.assertEqual("strategy_lab_experiment", artifacts[0]["artifact"])
        self.assertEqual(1, conn.execute("select count(*) from strategy_lab_experiments").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from strategy_owner_tasks").fetchone()[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
