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
    def test_default_capacity_supports_broad_paper_discovery(self):
        scanner = DEFAULT_SETTINGS["scanner"]
        strategy_lab = DEFAULT_SETTINGS["strategy_lab"]

        self.assertGreaterEqual(scanner["review_top"], 250)
        self.assertGreaterEqual(scanner["max_new_paper_trades"], 50)
        self.assertGreaterEqual(DEFAULT_SETTINGS["risk"]["max_open_paper_trades"], 500)
        self.assertGreaterEqual(strategy_lab["max_candidates_per_loop"], 100)
        self.assertGreaterEqual(strategy_lab["runtime_review_reserved_slots"], 50)

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
