import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import llm_bridge


class RecommendationSchemaTests(unittest.TestCase):
    def test_market_scout_contract_requires_single_json_object(self) -> None:
        schema = llm_bridge._recommendation_schema(["propose_code_change"])
        self.assertEqual(
            schema["required_fields"],
            list(llm_bridge.REQUIRED_RECOMMENDATION_FIELDS),
        )
        self.assertTrue(schema["validation_policy"]["publish_only_single_json_object"])
        self.assertTrue(schema["validation_policy"]["reject_missing_required_fields"])
        contract = schema["market_key_contracts"]["paper_system.integrity.market_scout"]
        self.assertIn("exactly one schema-complete top-level JSON object", contract)
        self.assertIn(
            "action, priority, title, rationale, market_key, evidence, and proposed_change",
            contract,
        )

    def test_market_scout_fallback_recommendation_is_schema_complete(self) -> None:
        schema = llm_bridge._recommendation_schema(["propose_code_change"])
        fallback = schema["fallback_recommendations"]["paper_system.integrity.market_scout"]
        self.assertEqual(set(fallback), set(llm_bridge.REQUIRED_RECOMMENDATION_FIELDS))
        self.assertEqual(fallback["action"], "hold")
        self.assertEqual(fallback["priority"], 50)
        self.assertEqual(fallback["market_key"], "paper_system.integrity.market_scout")
        self.assertEqual(fallback["evidence"], {"issue": "schema_validation_failed"})
        self.assertEqual(fallback["proposed_change"], {"goal": "preserve parser compatibility"})

    def test_schema_directs_incomplete_outputs_to_no_action(self) -> None:
        schema = llm_bridge._recommendation_schema(["propose_code_change", "no_action"])

        self.assertIn("action='no_action'", schema["fallback_behavior"])
        self.assertIn("behavioral_test", schema["code_change"]["runtime_integration"])
        self.assertIn("Import/existence-only tests are insufficient", schema["code_change"]["runtime_integration"])
