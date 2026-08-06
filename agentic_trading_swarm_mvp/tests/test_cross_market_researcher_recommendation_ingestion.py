import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_recommendation_ingestion import _json_objects, _native_payload


class CrossMarketResearcherRecommendationIngestionTests(unittest.TestCase):
    def test_native_payload_repairs_partial_cross_market_researcher_object(self) -> None:
        payload = _native_payload(
            {
                "parsed": {
                    "source_agent": "cross_market_researcher",
                    "market_key": "paper_global_macro",
                    "evidence": {"issue": "previous output was incomplete"},
                    "proposed_change": {"goal": "keep one strict object"},
                }
            }
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "diagnostic")
        self.assertEqual(payload["priority"], 90)
        self.assertEqual(payload["market_key"], "paper_global_macro")
        self.assertEqual(payload["title"], "Return a single complete paper-trading recommendation object")
        self.assertIn("paper-only", payload["rationale"])
        self.assertEqual(payload["evidence"]["issue"], "previous output was incomplete")
        self.assertEqual(
            payload["evidence"]["constraint"],
            "Output must remain paper-only and contain exactly one JSON object.",
        )
        self.assertTrue(payload["evidence"]["market_recommendation_blocked"])
        self.assertTrue(payload["evidence"]["insufficient_structured_evidence"])
        self.assertEqual(payload["proposed_change"]["goal"], "keep one strict object")
        self.assertEqual(
            payload["proposed_change"]["format_rule"],
            "No markdown, no commentary, no arrays, valid JSON only.",
        )
        self.assertIn("explicit cross-market support facts", payload["proposed_change"]["next_step"])

    def test_json_objects_repairs_single_cross_market_object(self) -> None:
        parsed = list(
            _json_objects(
                '{"source_agent":"cross_market_researcher","market_key":"paper_global_macro",'
                '"evidence":{"impact":"parser failure"},'
                '"proposed_change":{"safety_rule":"Paper-trading only; do not imply live execution."}}'
            )
        )

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["action"], "diagnostic")
        self.assertEqual(parsed[0]["priority"], 90)
        self.assertEqual(parsed[0]["evidence"]["impact"], "parser failure")
        self.assertTrue(parsed[0]["evidence"]["market_recommendation_blocked"])
        self.assertIn("required_fields", parsed[0]["proposed_change"])

    def test_native_payload_preserves_no_action_cross_market_schema_guard(self) -> None:
        payload = _native_payload(
            {
                "parsed": {
                    "source_agent": "cross_market_researcher",
                    "market_key": "paper_global_macro",
                    "action": "no_action",
                    "rationale": "Keep the malformed output diagnostic-only.",
                    "evidence": {"issue": "schema guard"},
                    "proposed_change": {"goal": "rerun the paper-only analysis"},
                }
            }
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "no_action")
        self.assertEqual(payload["priority"], 90)

    def test_json_objects_rejects_top_level_array_wrapper(self) -> None:
        parsed = list(
            _json_objects(
                '[{"source_agent":"cross_market_researcher","market_key":"paper_global_macro","evidence":{"issue":"x"}}]'
            )
        )
        self.assertEqual(parsed, [])


if __name__ == "__main__":
    unittest.main()
