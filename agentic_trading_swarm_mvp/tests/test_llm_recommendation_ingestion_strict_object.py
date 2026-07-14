import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_recommendation_ingestion import _json_objects, _provider_texts, _single_top_level_object_text


class StrictSingleObjectParsingTests(unittest.TestCase):
    def test_single_top_level_object_text_strips_json_fence(self):
        text = '```json\n{"action":"code_change","title":"x"}\n```'
        self.assertEqual(_single_top_level_object_text(text), '{"action":"code_change","title":"x"}')

    def test_single_top_level_object_text_rejects_top_level_array(self):
        text = '[{"action":"code_change","title":"x"}]'
        self.assertIsNone(_single_top_level_object_text(text))

    def test_json_objects_accepts_single_fenced_object(self):
        text = '```json\n{"action":"code_change","title":"x"}\n```'
        objects = list(_json_objects(text))
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["action"], "code_change")
        self.assertEqual(objects[0]["title"], "x")

    def test_json_objects_rejects_top_level_array_wrapper(self):
        text = '[{"action":"code_change","title":"x"}]'
        self.assertEqual(list(_json_objects(text)), [])

    def test_provider_texts_prefers_single_object_without_fences(self):
        response = {
            "output_text": '```json\n{"action":"code_change","title":"x"}\n```',
            "preview": "fallback text",
        }
        texts = _provider_texts(response)
        self.assertIn('{"action":"code_change","title":"x"}', texts)
        self.assertIn('```json\n{"action":"code_change","title":"x"}\n```', texts)
        self.assertIn("fallback text", texts)


if __name__ == "__main__":
    unittest.main()
