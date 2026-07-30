import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import _explicit_expected_files


class ExpectedFileNormalizationTests(unittest.TestCase):
    def test_extracts_normalized_paths_from_markdown_bullets(self) -> None:
        payload = {"expected_files": ["docs/ignored.md"]}
        code_change = {
            "expected_files": "- `src/self_improvement.py`\n- ./tests/test_code_evolution.py\n- README.md\n- `src/self_improvement.py`"
        }

        self.assertEqual(
            _explicit_expected_files(payload, code_change),
            [
                "src/self_improvement.py",
                "tests/test_code_evolution.py",
                "README.md",
            ],
        )

    def test_prefers_first_non_empty_expected_file_source(self) -> None:
        payload = {"expected_files": ["docs/should_not_win.md"]}
        code_change = {"expected_files": ["`src/self_improvement.py`"]}

        self.assertEqual(_explicit_expected_files(payload, code_change), ["src/self_improvement.py"])


if __name__ == "__main__":
    unittest.main()
