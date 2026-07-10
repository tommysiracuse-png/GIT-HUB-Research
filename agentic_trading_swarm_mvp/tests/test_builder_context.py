from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.builder_context import build_builder_context, render_builder_context  # noqa: E402


class BuilderContextTests(unittest.TestCase):
    def test_context_includes_exact_file_hash_symbols_and_likely_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "sample.py").write_text(
                "class Demo:\n"
                "    pass\n\n"
                "def run_demo():\n"
                "    return Demo()\n",
                encoding="utf-8",
            )

            context = build_builder_context(
                root,
                ["src/sample.py"],
                max_chars=1000,
                likely_tests=["python -m unittest tests/test_sample.py"],
            )
            rendered = render_builder_context(context)

        entry = context["files"][0]
        self.assertTrue(entry["exists"])
        self.assertIn("Demo", entry["symbols"])
        self.assertIn("run_demo", entry["symbols"])
        self.assertIn("sha256", entry)
        self.assertIn("BUILDER_CONTEXT version=1", rendered)
        self.assertIn("python -m unittest tests/test_sample.py", rendered)

    def test_context_records_missing_files_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = build_builder_context(pathlib.Path(tmp), ["src/missing.py"], max_chars=100)

        self.assertFalse(context["files"][0]["exists"])
        self.assertEqual(context["files"][0]["text"], "<missing file>")


if __name__ == "__main__":
    unittest.main()
