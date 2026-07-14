import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from code_evolution import validate_strict_recommendation_schema


class StrictRecommendationSchemaGuardTests(unittest.TestCase):
    def test_accepts_schema_complete_object_without_arrays(self) -> None:
        packet = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Enforce strict JSON output",
            "rationale": "Downstream parsers need one complete paper-only object.",
            "market_key": "paper_global_output_contract",
            "evidence": {
                "issue": "partial_response",
                "risk": "parser_failure",
            },
            "proposed_change": {
                "objective": "Return one complete object only.",
                "policy": "Reject partial output.",
            },
            "code_change": {
                "change_category": "evolution_loop_improvement",
                "implementation_mode": "runtime_active",
                "expected_files": "src/code_evolution.py, src/llm_bridge.py",
                "rollback_criteria": "Revert if parser compatibility regresses.",
            },
        }

        valid, error = validate_strict_recommendation_schema(packet)

        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_rejects_array_values_anywhere_in_packet(self) -> None:
        packet = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Reject array payloads",
            "rationale": "Arrays violate the strict single-object recommendation contract.",
            "market_key": "paper_global_output_contract",
            "evidence": {"issue": "array_payload"},
            "proposed_change": {
                "expected_files": ["src/code_evolution.py", "src/llm_bridge.py"],
            },
        }

        valid, error = validate_strict_recommendation_schema(packet)

        self.assertFalse(valid)
        self.assertIn("$.proposed_change.expected_files", error)

    def test_rejects_non_json_serializable_packets(self) -> None:
        packet = object()

        valid, error = validate_strict_recommendation_schema(packet)  # type: ignore[arg-type]

        self.assertFalse(valid)
        self.assertEqual(error, "packet must be a mapping")
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from code_evolution import validate_strict_recommendation_schema


def _base_packet() -> dict:
    return {
        "action": "hold",
        "priority": 60,
        "title": "Hold until the payload is complete",
        "rationale": "The prior response was incomplete and should not change paper allocations.",
        "market_key": "paper_cross_market_global",
        "evidence": {
            "status": "incomplete_prior_payload",
            "signal_quality": "unconfirmed",
        },
        "proposed_change": {
            "portfolio_action": "no_position_change",
        },
    }


class ValidateStrictRecommendationSchemaTests(unittest.TestCase):
    def test_accepts_complete_packet(self) -> None:
        valid, reason = validate_strict_recommendation_schema(_base_packet())
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_rejects_blank_market_key(self) -> None:
        packet = _base_packet()
        packet["market_key"] = "   "

        valid, reason = validate_strict_recommendation_schema(packet)

        self.assertFalse(valid)
        self.assertEqual(reason, "missing required fields: market_key")

    def test_rejects_empty_evidence_payload(self) -> None:
        packet = _base_packet()
        packet["evidence"] = {"status": ""}

        valid, reason = validate_strict_recommendation_schema(packet)

        self.assertFalse(valid)
        self.assertEqual(reason, "evidence must contain at least one non-empty value")

    def test_rejects_empty_proposed_change_payload(self) -> None:
        packet = _base_packet()
        packet["proposed_change"] = {"portfolio_action": ""}

        valid, reason = validate_strict_recommendation_schema(packet)

        self.assertFalse(valid)
        self.assertEqual(reason, "proposed_change must contain at least one non-empty value")

    def test_rejects_out_of_range_priority(self) -> None:
        packet = _base_packet()
        packet["priority"] = 0

        valid, reason = validate_strict_recommendation_schema(packet)

        self.assertFalse(valid)
        self.assertEqual(reason, "priority must be an integer between 1 and 100")


if __name__ == "__main__":
    unittest.main()
