from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.builder_context import build_builder_context, render_builder_context, resolve_repo_targets  # noqa: E402


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

    def test_repo_capability_map_resolves_conceptual_path_from_real_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "market_admission.py").write_text(
                "def advance_market_admission():\n    return 'paper'\n", encoding="utf-8"
            )
            (root / "tests" / "test_market_admission.py").write_text(
                "def test_advance_market_admission():\n    assert True\n", encoding="utf-8"
            )
            resolved = resolve_repo_targets(
                root,
                {
                    "title": "Advance market admission",
                    "proposed_change": "Wire admission progress into paper evaluation.",
                    "change_category": "runtime_pipeline_integration",
                },
                conceptual_paths=["src/runtime/market_admission_pipeline.py"],
            )
            self.assertEqual(["src/market_admission.py"], resolved["source_files"])
            self.assertEqual(["tests/test_market_admission.py"], resolved["test_files"])


if __name__ == "__main__":
    unittest.main()
