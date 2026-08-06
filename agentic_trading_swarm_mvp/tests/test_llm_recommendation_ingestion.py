import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_recommendation_ingestion import (
    RecommendationIngestor,
    normalize_recommendation,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_recommendations"


def fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class RecommendationIngestionTest(unittest.TestCase):
    def test_native_structured_recommendation_is_valid(self):
        result = normalize_recommendation(fixture("native_valid.json"))

        self.assertTrue(result["accepted"])
        self.assertEqual("native_valid", result["parse_status"])
        self.assertEqual("code_change", result["action"])
        self.assertEqual(97, result["priority"])
        self.assertEqual("code_evolution", result["downstream_task_type"])
        self.assertTrue(result["recommendation_id"].startswith("rec_"))
        self.assertLessEqual(len(result["raw_preview"]), 800)

    def test_embedded_complete_json_overrides_fallback_wrapper(self):
        result = normalize_recommendation(fixture("embedded_valid.json"))

        self.assertTrue(result["accepted"])
        self.assertEqual("recovered_valid", result["parse_status"])
        self.assertEqual("code_change", result["action"])
        self.assertEqual(91, result["priority"])
        self.assertEqual("Repair evolution handoff", result["title"])

    def test_truncated_json_is_not_repaired_heuristically(self):
        result = normalize_recommendation(fixture("truncated.json"))

        self.assertFalse(result["accepted"])
        self.assertTrue(result["quarantined"])
        self.assertEqual("truncated_json", result["parse_status"])
        self.assertNotIn("downstream_task_type", result)

    def test_unsafe_recommendation_is_rejected_after_native_parse(self):
        result = normalize_recommendation(fixture("unsafe_action.json"))

        self.assertFalse(result["accepted"])
        self.assertEqual("safety_rejected", result["parse_status"])
        self.assertEqual("live_trading", result["terminal_failure_reason"])

    def test_one_schema_retry_can_recover_truncated_response(self):
        calls = []

        def retry(prompt):
            calls.append(prompt)
            return fixture("native_valid.json")

        ingestor = RecommendationIngestor(retry_timeout_seconds=1)
        result = ingestor.ingest(fixture("truncated.json"), retry=retry)

        self.assertTrue(result["accepted"])
        self.assertEqual(1, result["retry_count"])
        self.assertEqual("truncated_json", result["initial_parse_status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, ingestor.audit()["retried"])

    def test_failed_retry_is_quarantined_and_not_retried_again(self):
        calls = []

        def retry(prompt):
            calls.append(prompt)
            return fixture("truncated.json")

        ingestor = RecommendationIngestor(retry_timeout_seconds=1)
        result = ingestor.ingest(fixture("truncated.json"), retry=retry)

        self.assertFalse(result["accepted"])
        self.assertTrue(result["quarantined"])
        self.assertEqual(1, result["retry_count"])
        self.assertEqual(1, len(calls))

    def test_durable_ledger_deduplicates_replays(self):
        dispatched = []

        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "recommendations.jsonl"
            first_ingestor = RecommendationIngestor(ledger_path=ledger)
            first = first_ingestor.ingest(fixture("native_valid.json"))
            first_ingestor.dispatch(
                first, lambda item, task: dispatched.append((item, task))
            )

            second_ingestor = RecommendationIngestor(ledger_path=ledger)
            second = second_ingestor.ingest(fixture("native_valid.json"))
            second_ingestor.dispatch(
                second, lambda item, task: dispatched.append((item, task))
            )

        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertEqual("deduplicated", second["ingestion_status"])
        self.assertEqual(1, len(dispatched))

    def test_role_and_priority_are_validated(self):
        payload = fixture("native_valid.json")
        payload["output_parsed"]["agent_role"] = "broker_operator"
        payload["output_parsed"]["priority"] = 101

        result = normalize_recommendation(payload)

        self.assertFalse(result["accepted"])
        self.assertEqual("role_rejected", result["parse_status"])

    def test_target_and_test_command_safety_checks_apply_after_recovery(self):
        wrapper = {
            "parser": "fallback",
            "agent_role": "code_evolution",
            "raw_response": json.dumps(
                {
                    "action": "code_change",
                    "priority": 80,
                    "title": "Unsafe target",
                    "rationale": "Attempts to leave the repository.",
                    "proposed_change": "Modify the target.",
                    "agent_role": "code_evolution",
                    "target_files": ["../outside.py"],
                    "tests_to_run": ["python -m unittest tests.test_example"],
                }
            ),
        }

        result = normalize_recommendation(wrapper)

        self.assertFalse(result["accepted"])
        self.assertEqual("safety_rejected", result["parse_status"])
        self.assertEqual("invalid_target_files", result["terminal_failure_reason"])

    def test_execution_route_hunter_partial_payload_fails_closed_to_no_action_fallback(self):
        result = normalize_recommendation(
            {
                "parsed": {
                    "market_key": "paper.execution_route_hunter",
                    "action": "route_review",
                    "evidence": {"issue": "route object was incomplete"},
                    "proposed_change": {"summary": "adjust route"},
                    "agent_role": "route_resolver",
                }
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("native_valid", result["parse_status"])
        self.assertEqual("no_action", result["action"])
        self.assertEqual("no_action", result["downstream_task_type"])
        self.assertEqual("paper.execution_route_hunter", result["market_key"])
        self.assertIn("missing_required_fields", result["evidence"]["schema_violation"])
        self.assertTrue(result["evidence"]["explicit_paper_safe_route_required"])

    def test_execution_route_hunter_actionable_payload_requires_explicit_paper_safe_route(self):
        result = normalize_recommendation(
            {
                "parsed": {
                    "market_key": "paper.execution_route_hunter",
                    "action": "route_review",
                    "priority": 88,
                    "title": "Route review",
                    "rationale": "Change the route after validation.",
                    "evidence": {"issue": "route costs changed"},
                    "proposed_change": {"summary": "adjust route"},
                    "agent_role": "route_resolver",
                }
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("no_action", result["action"])
        self.assertEqual(
            "missing_explicit_paper_safe_route",
            result["evidence"]["schema_violation"],
        )

    def test_execution_route_hunter_accepts_actionable_payload_with_explicit_paper_safe_route(self):
        result = normalize_recommendation(
            {
                "parsed": {
                    "market_key": "paper.execution_route_hunter",
                    "action": "route_review",
                    "priority": 88,
                    "title": "Use validated paper proxy route",
                    "rationale": "A maintained paper proxy route is already validated.",
                    "evidence": {
                        "paper_safe_route": {
                            "route_id": "okx_derivatives_paper",
                            "route_status": "paper_testable_proxy",
                            "paper_only": True,
                        }
                    },
                    "proposed_change": {"summary": "review the validated proxy route"},
                    "agent_role": "route_resolver",
                }
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("route_review", result["action"])
        self.assertEqual("route_review", result["downstream_task_type"])

    def test_cross_market_researcher_partial_payload_normalizes_to_blocked_paper_only_diagnostic(self):
        result = normalize_recommendation(
            {
                "parsed": {
                    "source_agent": "cross_market_researcher",
                    "market_key": "paper_global_macro",
                    "evidence": {"issue": "previous output was incomplete"},
                    "proposed_change": {"goal": "keep one strict object"},
                }
            }
        )

        self.assertTrue(result["accepted"])
        self.assertEqual("native_valid", result["parse_status"])
        self.assertEqual("propose_diagnostic_hypothesis", result["action"])
        self.assertEqual("diagnostic", result["downstream_task_type"])
        self.assertEqual("market_researcher", result["agent_role"])
        self.assertTrue(result["evidence"]["market_recommendation_blocked"])
        self.assertTrue(result["evidence"]["insufficient_structured_evidence"])
        self.assertTrue(result["proposed_change"]["paper_only"])


if __name__ == "__main__":
    unittest.main()
