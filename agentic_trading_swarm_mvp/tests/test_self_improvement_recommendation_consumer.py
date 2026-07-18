import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import (  # noqa: E402
    RECOMMENDATION_REJECTION_TTL_SECONDS,
    _CONSUMER_REJECTION_CACHE,
    _normalize_code_change_recommendation,
)


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


if __name__ == "__main__":
    unittest.main()
