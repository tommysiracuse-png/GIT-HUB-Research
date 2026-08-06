import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.code_evolution import (
    coerce_paper_only_recommendation,
    validate_strict_recommendation_json_text,
)


class CodeEvolutionContractTests(unittest.TestCase):
    def test_validate_strict_recommendation_json_text_accepts_single_paper_only_object(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Valid packet",
            "rationale": "OK",
            "market_key": "paper_execution_route_hunter",
            "paper_only": True,
            "evidence": {
                "constraint": "paper_only",
                "issue": "unit_test",
                "risk": "schema_guard",
            },
            "proposed_change": {
                "goal": "Keep parser compatibility",
                "constraints": "Emit one JSON object only",
            },
        }

        valid, parsed, reason = validate_strict_recommendation_json_text(
            json.dumps(payload)
        )

        self.assertTrue(valid)
        self.assertEqual(parsed, payload)
        self.assertEqual(reason, "ok")

    def test_validate_strict_recommendation_json_text_rejects_truncation_and_trailing_text(self) -> None:
        valid, parsed, reason = validate_strict_recommendation_json_text(
            '{"action":"propose_code_change"'
        )
        self.assertFalse(valid)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "truncated_json")

        valid, parsed, reason = validate_strict_recommendation_json_text(
            '{"action":"hold","priority":1,"title":"t","rationale":"r","market_key":"paper.x",'
            '"paper_only":true,"evidence":{"constraint":"paper_only","issue":"i","risk":"r"},'
            '"proposed_change":{"goal":"g","constraints":"c"}} trailing'
        )
        self.assertFalse(valid)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "extraneous_text")

    def test_coerce_paper_only_recommendation_rejects_truncated_output(self) -> None:
        result = coerce_paper_only_recommendation('{"action":"propose_code_change"')
        self.assertEqual(result["market_key"], "paper.execution_route_hunter.output_contract")
        self.assertIn("fallback", result["title"].lower())

    def test_coerce_paper_only_recommendation_passes_through_valid_packet(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Valid packet",
            "rationale": "OK",
            "market_key": "paper.global.execution_route_hunter",
            "evidence": {"source": "unit_test"},
            "proposed_change": {"summary": "Keep paper-only"},
        }
        self.assertEqual(coerce_paper_only_recommendation(payload), payload)

    def test_coerce_paper_only_recommendation_falls_back_on_schema_violation(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 60,
            "title": "Bad packet",
            "rationale": "OK",
            "market_key": "paper.global.execution_route_hunter",
            "evidence": [],
            "proposed_change": {"summary": "Keep paper-only"},
        }
        result = coerce_paper_only_recommendation(payload)
        self.assertEqual(result["priority"], 0)
        self.assertIn("evidence must be a JSON object", result["evidence"]["schema_error"])
