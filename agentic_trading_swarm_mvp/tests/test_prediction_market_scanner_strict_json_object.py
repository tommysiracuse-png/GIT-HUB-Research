import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from prediction_market_scanner import _prediction_risk_flags
from strict_json_object import (
    coerce_single_json_object,
    parse_single_json_object,
    validate_single_json_object,
)


class StrictJsonObjectTests(unittest.TestCase):
    def test_parse_single_json_object_accepts_complete_object_string(self):
        payload = parse_single_json_object(
            '{"action":"propose_code_change","priority":90}',
            required_keys=("action", "priority"),
        )
        self.assertEqual("propose_code_change", payload["action"])
        self.assertEqual(90, payload["priority"])

    def test_parse_single_json_object_rejects_arrays_and_markdown(self):
        self.assertIsNone(parse_single_json_object('[{"action":"propose_code_change"}]'))
        self.assertIsNone(parse_single_json_object('```json\n{"action":"propose_code_change"}\n```'))

    def test_validate_single_json_object_requires_top_level_keys(self):
        with self.assertRaisesRegex(ValueError, "missing required top-level keys: priority"):
            validate_single_json_object(
                {"action": "propose_code_change"},
                required_keys=("action", "priority"),
                context="recommendation",
            )

    def test_coerce_single_json_object_returns_default_for_preview_fragment(self):
        default = {"fallback": True}
        payload = coerce_single_json_object("preview: {action: propose_code_change}", default=default)
        self.assertEqual(default, payload)

    def test_prediction_risk_flags_tolerates_non_object_metadata(self):
        flags = _prediction_risk_flags(
            None,
            600.0,
            50.0,
            '["preview-fragment"]',
        )
        self.assertIn("resolution_date_unclear", flags)
        self.assertIn("wide_prediction_spread", flags)
        self.assertIn("thin_prediction_liquidity", flags)


if __name__ == "__main__":
    unittest.main()
