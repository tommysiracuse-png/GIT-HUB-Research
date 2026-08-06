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
    add_ingestion_audit,
    attempt_bounded_preflight_repair,
    consume_swarm_payload,
)


FIXTURES = Path(__file__).parent / "fixtures" / "llm_recommendations"


def fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class SelfImprovementRecommendationFlowTest(unittest.TestCase):
    def test_paper_only_canary_reaches_full_parse_success_with_single_retry(self):
        ingestor = RecommendationIngestor(retry_timeout_seconds=1)
        sample = [
            fixture("native_valid.json"),
            fixture("embedded_valid.json"),
            fixture("truncated.json"),
        ]
        retry_calls = []

        def retry(_prompt):
            retry_calls.append("retry")
            repaired = json.loads(json.dumps(fixture("native_valid.json")))
            repaired["output_parsed"]["title"] = "Recovered truncated recommendation"
            repaired["output_parsed"]["rationale"] = (
                "The retry returned one schema-complete paper-only recommendation object."
            )
            return repaired

        results = [ingestor.ingest(item, retry=retry) for item in sample]

        self.assertEqual(3, len(results))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(1, len(retry_calls))
        self.assertEqual(
            1.0,
            sum(1 for result in results if result["accepted"]) / len(results),
        )
        self.assertEqual("truncated_json", results[2]["initial_parse_status"])
        self.assertEqual(1, ingestor.audit()["retried"])

    def test_offline_replay_routes_valid_code_change_exactly_once(self):
        dispatches = []
        swarm = {
            "model_metadata": {"provider": "offline-fixture", "model": "test"},
            "provenance": {"source": "llm_swarm_latest"},
            "recommendations": [
                fixture("embedded_valid.json"),
                fixture("embedded_valid.json"),
                fixture("truncated.json"),
                fixture("unsafe_action.json"),
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            ingestor = RecommendationIngestor(
                ledger_path=Path(directory) / "ledger.jsonl"
            )
            results = consume_swarm_payload(
                swarm,
                ingestor=ingestor,
                dispatcher=lambda item, task: dispatches.append(
                    (item["recommendation_id"], task)
                ),
            )

        self.assertEqual(4, len(results))
        self.assertEqual(1, len(dispatches))
        self.assertEqual("code_evolution", dispatches[0][1])
        self.assertEqual("deduplicated", results[1]["ingestion_status"])
        self.assertTrue(results[2]["quarantined"])
        self.assertTrue(results[3]["quarantined"])

        audit = ingestor.audit()
        self.assertEqual(1, audit["deduplications"])
        self.assertEqual(2, audit["quarantined"])
        self.assertEqual(1, audit["dispatched"])
        self.assertEqual(
            1, audit["by_downstream_task_type"]["code_evolution"]
        )

    def test_known_preflight_failure_receives_only_one_repair(self):
        recommendation = {
            "recommendation_id": "rec_test",
            "action": "code_change",
        }
        preflight_calls = []
        repair_calls = []

        def preflight(candidate):
            preflight_calls.append(candidate)
            if candidate.get("patch"):
                return {"ok": True, "changed_files": ["code_evolution.py"]}
            return {
                "ok": False,
                "failure_reason": "missing_unified_diff",
                "changed_files": [],
            }

        def repair(candidate, failure):
            repair_calls.append((candidate, failure))
            repaired = dict(candidate)
            repaired["patch"] = (
                "--- a/code_evolution.py\n"
                "+++ b/code_evolution.py\n"
                "@@ -1 +1 @@\n-old\n+new\n"
            )
            return repaired

        result = attempt_bounded_preflight_repair(
            recommendation,
            preflight=preflight,
            repair=repair,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["repair_attempted"])
        self.assertEqual(1, result["repair_count"])
        self.assertEqual(1, len(repair_calls))
        self.assertEqual(2, len(preflight_calls))
        self.assertEqual(
            "missing_unified_diff", result["initial_failure_reason"]
        )

    def test_unknown_preflight_failure_is_not_repaired(self):
        repair_calls = []

        result = attempt_bounded_preflight_repair(
            {"recommendation_id": "rec_test"},
            preflight=lambda candidate: {
                "ok": False,
                "failure_reason": "unapproved_failure",
            },
            repair=lambda candidate, failure: repair_calls.append(failure),
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["repair_attempted"])
        self.assertEqual([], repair_calls)
        self.assertEqual("unapproved_failure", result["terminal_failure_reason"])

    def test_ingestion_audit_can_be_shared_with_reports_and_state_packets(self):
        ingestor = RecommendationIngestor()
        accepted = ingestor.ingest(fixture("native_valid.json"))
        ingestor.dispatch(accepted, lambda item, task: None)
        ingestor.ingest(fixture("truncated.json"))

        report = add_ingestion_audit(
            {"mode": "paper", "live_trading_allowed": False}, ingestor
        )
        packet = add_ingestion_audit(
            {
                "purpose": "Read-only LLM state",
                "mode": "paper",
                "live_trading_allowed": False,
            },
            ingestor.audit(),
        )

        self.assertEqual(
            report["recommendation_ingestion"],
            packet["recommendation_ingestion"],
        )
        self.assertEqual(
            1, report["recommendation_ingestion"]["native"]
        )
        self.assertEqual(
            1, report["recommendation_ingestion"]["quarantined"]
        )
        self.assertFalse(report["live_trading_allowed"])
        self.assertFalse(packet["live_trading_allowed"])


if __name__ == "__main__":
    unittest.main()
