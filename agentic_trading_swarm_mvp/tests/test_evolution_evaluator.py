from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evolution.evaluator import benchmark_builder_change, classify_sandbox_failure, evaluate_candidate  # noqa: E402


class EvolutionEvaluatorTests(unittest.TestCase):
    def test_classifies_patch_apply_separately_from_unit_test_failures(self) -> None:
        self.assertEqual(
            classify_sandbox_failure({"stage": "patch_check", "passed": False}),
            "discarded_patch_apply_failure",
        )
        self.assertEqual(
            classify_sandbox_failure({"stage": "patch_apply", "passed": False}),
            "discarded_patch_apply_failure",
        )

    def test_classifies_invalid_generated_test_commands(self) -> None:
        sandbox = {
            "stage": "tests",
            "passed": False,
            "commands": [{"stderr_tail": "ModuleNotFoundError: No module named 'tests.test_missing'"}],
        }

        self.assertEqual(classify_sandbox_failure(sandbox), "discarded_invalid_test_command")

    def test_classifies_real_test_failure(self) -> None:
        sandbox = {
            "stage": "tests",
            "passed": False,
            "commands": [{"stderr_tail": "AssertionError: expected 1 got 2"}],
        }

        self.assertEqual(classify_sandbox_failure(sandbox), "discarded_test_failure")

    def test_candidate_gate_requires_canary_success(self) -> None:
        gate = evaluate_candidate(
            sandbox={"passed": True},
            canary={"passed": False, "reason": "radar failed"},
            category="scanner_expansion",
            changed_files=["src/frontier_crypto_adapter.py"],
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "archived_failed")
        self.assertEqual(gate["reason"], "canary_failed")

    def test_adapter_candidate_must_have_a_capability_change(self) -> None:
        gate = evaluate_candidate(
            sandbox={"passed": True},
            canary={"passed": True},
            category="public_data_adapter",
            changed_files=[],
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "no_capability_change")

    def test_builder_benchmark_requires_non_negative_uplift(self) -> None:
        self.assertTrue(benchmark_builder_change({"before_solve_rate": 0.2, "after_solve_rate": 0.3})["passed"])
        self.assertFalse(benchmark_builder_change({"before_solve_rate": 0.3, "after_solve_rate": 0.2})["passed"])


if __name__ == "__main__":
    unittest.main()
