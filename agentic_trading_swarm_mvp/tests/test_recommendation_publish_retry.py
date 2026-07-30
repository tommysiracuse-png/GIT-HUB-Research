import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from self_improvement import _allowed_expected_file


class AllowedExpectedFileTests(unittest.TestCase):
    def test_accepts_markdown_wrapped_allowed_paths(self) -> None:
        self.assertTrue(_allowed_expected_file("- `src/self_improvement.py`"))
        self.assertTrue(_allowed_expected_file("* ./tests/test_code_evolution.py"))
        self.assertTrue(_allowed_expected_file("`README.md`"))

    def test_rejects_traversal_and_absolute_paths_after_normalization(self) -> None:
        for path in (
            "../outside.py",
            "* ../README.md",
            "/tmp/evil.py",
            "C:\\temp\\evil.py",
            "docs/../README.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(_allowed_expected_file(path))


if __name__ == "__main__":
    unittest.main()
