from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import research_worker  # noqa: E402


class ResearchWorkerTests(unittest.TestCase):
    def test_evidence_bundle_preserves_source_and_blocks_runtime_use_until_canary(self) -> None:
        bundle = research_worker.evidence_bundle(
            "https://example.com/docs",
            "Public ticker endpoint exists.",
            "Potential new market venue.",
            "Add parser fixture and pass adapter canary.",
        )

        self.assertEqual(bundle["source_url"], "https://example.com/docs")
        self.assertEqual(bundle["allowed_use"], "research_only_until_adapter_canary_passes")
        self.assertIn("captured_at", bundle)

    def test_run_once_writes_read_only_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_json = research_worker.REPORT_JSON
            old_md = research_worker.REPORT_MD
            old_runs = research_worker.RUNS_DIR
            try:
                research_worker.RUNS_DIR = pathlib.Path(tmp)
                research_worker.REPORT_JSON = pathlib.Path(tmp) / "research_worker_latest.json"
                research_worker.REPORT_MD = pathlib.Path(tmp) / "research_worker_report.md"

                report = research_worker.run_once()

                self.assertEqual(report["status"], "idle")
                self.assertTrue(research_worker.REPORT_JSON.exists())
                saved = json.loads(research_worker.REPORT_JSON.read_text(encoding="utf-8"))
                self.assertEqual(saved["hard_rule"], report["hard_rule"])
                self.assertIn("canary validation", research_worker.REPORT_MD.read_text(encoding="utf-8"))
            finally:
                research_worker.REPORT_JSON = old_json
                research_worker.REPORT_MD = old_md
                research_worker.RUNS_DIR = old_runs


if __name__ == "__main__":
    unittest.main()
