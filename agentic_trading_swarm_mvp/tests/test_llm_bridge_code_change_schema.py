import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_bridge import (  # noqa: E402
    CODE_CHANGE_ACTIONABLE_FIELDS,
    CODE_CHANGE_OPTIONAL_DETAIL_FIELDS,
    _recommendation_schema,
)


class RecommendationSchemaCodeChangeTests(unittest.TestCase):
    def test_code_change_schema_requires_actionable_fields(self) -> None:
        schema = _recommendation_schema(["propose_code_change", "hold"])
        code_change = schema["code_change"]

        self.assertEqual(
            code_change["required_actionable_fields"],
            list(CODE_CHANGE_ACTIONABLE_FIELDS),
        )
        self.assertEqual(
            code_change["detail_fields_to_prefer"],
            list(CODE_CHANGE_OPTIONAL_DETAIL_FIELDS),
        )

    def test_code_change_schema_warns_about_top_level_only_details(self) -> None:
        schema = _recommendation_schema(["propose_code_change"])
        code_change = schema["code_change"]

        self.assertIn(
            "duplicate the same values under code_change",
            code_change["mirror_top_level_when_nested_omitted"],
        )
        self.assertIn("hold/refine", code_change["partial_output_policy"])
        self.assertIn("Sparse", code_change["field_quality_gate"])
