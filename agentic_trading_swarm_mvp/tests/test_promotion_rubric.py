import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import code_evolution
import storage


def _settings() -> dict:
    return {
        "allow_live_trading": False,
        "code_evolution": {
            "enabled": True,
            "min_priority": 80,
            "generate_patch_when_missing": True,
            "require_frontier_model": False,
        },
    }


def _proposal(**overrides: object) -> dict:
    payload = {
        "action": "propose_code_change",
        "agent_name": "strategy_implementation_owner",
        "priority": 91,
        "title": "Promote a tested paper gate",
        "rationale": "Apply the observed paper quality condition to the strategy runtime.",
        "change_category": "strategy_lab_promotion",
        "implementation_mode": "runtime_active",
        "expected_files": ["src/strategy_lab.py", "tests/test_strategy_lab.py"],
        "tests_to_run": ["python -m unittest tests.test_strategy_lab"],
        "proposed_change": "Enforce the observed paper quality condition before promotion.",
        "evidence": {"quality_evidence": "paper evaluation: 40 exact-surface observations passed the gate"},
        "paper_testable_surface": "paper:okx:perp:BTC-USDT",
        "behavioral_gate": "Reject candidates when the exact-surface quality score is below 0.70.",
        "rollback_criteria": "Revert when the paper test fails or radar health declines after the gate.",
    }
    payload.update(overrides)
    return payload


class PromotionRubricTests(unittest.TestCase):
    def test_missing_surface_gate_and_evidence_are_quarantined_before_model_call(self) -> None:
        payload = _proposal(
            paper_testable_surface=None,
            behavioral_gate=None,
            rollback_criteria=None,
            evidence={"summary": "generic strategy idea"},
        )

        preflight = code_evolution.preflight_proposal(payload, _settings())

        rubric = preflight["quality_scorecard"]["promotion_rubric"]
        self.assertEqual("quarantined", rubric["status"])
        self.assertIn("missing_exact_paper_testable_surface", rubric["reasons"])
        self.assertIn("missing_behavioral_gate", rubric["reasons"])
        self.assertIn("missing_rollback_criterion", rubric["reasons"])
        self.assertIn("missing_route_or_quality_evidence", rubric["reasons"])
        self.assertEqual("quarantined_preflight_promotion_rubric", preflight["quality_scorecard"]["preflight_reject_status"])

    def test_exact_surface_gate_rollback_and_quality_evidence_pass_rubric(self) -> None:
        preflight = code_evolution.preflight_proposal(_proposal(), _settings())

        rubric = preflight["quality_scorecard"]["promotion_rubric"]
        self.assertTrue(rubric["passed"])
        self.assertTrue(rubric["has_quality_evidence"])
        self.assertEqual([], rubric["reasons"])

    def test_rename_only_strategy_is_quarantined_even_with_other_fields(self) -> None:
        payload = _proposal(
            title="Rename existing strategy label",
            rationale="Rename the existing strategy alias for readability.",
            proposed_change="Rename existing strategy label only.",
        )

        preflight = code_evolution.preflight_proposal(payload, _settings())

        self.assertIn(
            "strategy_rename_without_behavior_change",
            preflight["quality_scorecard"]["promotion_rubric"]["reasons"],
        )

    def test_quarantine_is_persisted_and_reported_without_model_generation(self) -> None:
        payload = _proposal(paper_testable_surface=None)
        with tempfile.TemporaryDirectory() as directory:
            conn = sqlite3.connect(pathlib.Path(directory) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_json, old_md = code_evolution.REPORT_JSON, code_evolution.REPORT_MD
            code_evolution.REPORT_JSON = pathlib.Path(directory) / "evolution.json"
            code_evolution.REPORT_MD = pathlib.Path(directory) / "evolution.md"
            try:
                with mock.patch.object(code_evolution, "generate_patch_with_frontier_model") as generate:
                    artifacts = code_evolution.process_code_change_recommendation(
                        conn,
                        {"recommendation_id": "rubric-missing-surface", "title": payload["title"], "payload": payload},
                        _settings(),
                    )
                report = code_evolution.write_code_evolution_reports(conn, _settings())
            finally:
                code_evolution.REPORT_JSON, code_evolution.REPORT_MD = old_json, old_md
                conn.close()

        self.assertEqual("quarantined_preflight_promotion_rubric", artifacts[0]["status"])
        self.assertEqual(0, generate.call_count)
        self.assertEqual(1, report["summary"]["promotion_rubric_candidate_count"])
        self.assertEqual(0, report["summary"]["promotion_rubric_pass_count"])
        self.assertEqual(1, report["summary"]["promotion_rubric_quarantine_count"])
        self.assertEqual(1.0, report["summary"]["promotion_rubric_quarantine_rate"])
        self.assertEqual(1, report["summary"]["duplicate_or_low_utility_promotion_count"])
        self.assertEqual(
            1,
            report["summary"]["promotion_rubric_reason_counts"]["missing_exact_paper_testable_surface"],
        )


if __name__ == "__main__":
    unittest.main()
