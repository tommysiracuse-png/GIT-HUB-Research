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
