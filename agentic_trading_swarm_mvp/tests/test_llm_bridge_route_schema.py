import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_bridge


class RecommendationSchemaRouteHunterTests(unittest.TestCase):
    def test_execution_route_hunter_fallback_is_schema_complete(self) -> None:
        schema = llm_bridge._recommendation_schema(["refine", "hold", "propose_code_change"])
        fallback = schema["fallback_recommendations"]["paper.execution_route_hunter"]

        self.assertEqual(fallback["market_key"], "paper.execution_route_hunter")
        for field in llm_bridge.REQUIRED_RECOMMENDATION_FIELDS:
            self.assertIn(field, fallback)
        self.assertEqual(fallback["action"], "refine")
        self.assertTrue(fallback["evidence"]["paper_only"])

    def test_execution_route_hunter_policy_is_paper_only(self) -> None:
        schema = llm_bridge._recommendation_schema(["refine", "hold", "propose_code_change"])

        self.assertEqual(schema["market_key"], "required stable routing key")
        self.assertTrue(schema["validation_policy"]["publish_only_single_json_object"])
        self.assertTrue(schema["validation_policy"]["reject_wrapper_arrays"])
        self.assertEqual(
            schema["validation_policy"]["paper_execution_route_hunter_fallback"],
            "refine_or_hold_with_validation_evidence",
        )
        self.assertEqual(schema["paper_safety_policies"]["paper.execution_route_hunter"]["mode"], "paper_only")
        self.assertTrue(schema["paper_safety_policies"]["paper.execution_route_hunter"]["forbid_live_execution_wording"])
        self.assertIn("validation failure captured in evidence", schema["market_key_contracts"]["paper.execution_route_hunter"])
