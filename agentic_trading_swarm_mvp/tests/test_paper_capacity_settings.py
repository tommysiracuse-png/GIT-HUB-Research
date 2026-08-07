import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from settings import DEFAULT_SETTINGS  # noqa: E402


class PaperCapacitySettingsTests(unittest.TestCase):
    def test_default_capacity_is_bounded_for_evidence_quality(self):
        scanner = DEFAULT_SETTINGS["scanner"]
        strategy_lab = DEFAULT_SETTINGS["strategy_lab"]

        self.assertGreater(scanner["review_top"], 0)
        self.assertLessEqual(scanner["review_top"], 100)
        self.assertGreater(scanner["max_new_paper_trades"], 0)
        self.assertLessEqual(scanner["max_new_paper_trades"], 10)
        self.assertLessEqual(DEFAULT_SETTINGS["risk"]["max_open_paper_trades"], 100)
        self.assertLessEqual(strategy_lab["max_candidates_per_loop"], 30)
        self.assertLessEqual(strategy_lab["runtime_review_reserved_slots"], 15)

    def test_example_config_matches_runtime_capacity(self):
        example = json.loads((ROOT / "config" / "settings.example.json").read_text(encoding="utf-8-sig"))

        self.assertEqual(DEFAULT_SETTINGS["scanner"]["review_top"], example["scanner"]["review_top"])
        self.assertEqual(
            DEFAULT_SETTINGS["scanner"]["max_new_paper_trades"],
            example["scanner"]["max_new_paper_trades"],
        )
        self.assertEqual(
            DEFAULT_SETTINGS["risk"]["max_open_paper_trades"],
            example["risk"]["max_open_paper_trades"],
        )
        self.assertEqual(
            DEFAULT_SETTINGS["strategy_lab"]["runtime_review_reserved_slots"],
            example["strategy_lab"]["runtime_review_reserved_slots"],
        )


if __name__ == "__main__":
    unittest.main()
