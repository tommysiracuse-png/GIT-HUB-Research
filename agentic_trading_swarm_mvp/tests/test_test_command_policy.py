import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement


class TestCommandPolicyTests(unittest.TestCase):
    def test_normalizes_unittest_paths_and_records_repair(self):
        payload = {
            "tests_to_run": ["`python -m unittest tests/test_code_evolution_runner.py`"],
        }

        normalized = self_improvement._normalize_code_change_test_commands(payload)

        self.assertEqual(
            normalized["tests_to_run"],
            ["python -m unittest tests.test_code_evolution_runner"],
        )
        self.assertEqual(
            normalized["test_command"],
            "python -m unittest tests.test_code_evolution_runner",
        )
        self.assertTrue(normalized["consumer_validation"]["normalized_test_commands"])
        self.assertTrue(normalized["test_command_policy"]["repaired"])
        self.assertFalse(normalized["test_command_policy"]["used_fallback"])

    def test_invalid_shell_command_falls_back_to_safe_suite(self):
        payload = {
            "tests_to_run": [
                "python -m unittest tests.test_code_evolution_runner && echo blocked",
            ],
        }

        normalized = self_improvement._normalize_code_change_test_commands(payload)

        self.assertEqual(
            normalized["tests_to_run"],
            list(self_improvement._SAFE_CODE_EVOLUTION_TEST_FALLBACK),
        )
        self.assertTrue(normalized["test_command_policy"]["used_fallback"])
        self.assertEqual(
            normalized["test_command_policy"]["fallback_reason"],
            "missing_or_invalid_unittest_command",
        )
        self.assertIn(
            "python -m unittest tests.test_code_evolution_runner && echo blocked",
            normalized["test_command_policy"]["rejected_test_commands"],
        )

    def test_process_code_change_recommendation_passes_normalized_payload(self):
        rec = {
            "payload": {
                "autonomous_plan": {
                    "tests_to_run": ["python -m unittest tests/test_test_command_policy.py"],
                }
            }
        }

        with mock.patch.object(
            self_improvement,
            "_process_code_change_recommendation",
            return_value={"status": "ok"},
        ) as patched:
            result = self_improvement.process_code_change_recommendation(None, rec)

        self.assertEqual(result, {"status": "ok"})
        forwarded = patched.call_args.args[1]
        self.assertEqual(
            forwarded["payload"]["tests_to_run"],
            ["python -m unittest tests.test_test_command_policy"],
        )
        self.assertEqual(
            forwarded["payload"]["autonomous_plan"]["tests_to_run"],
            ["python -m unittest tests.test_test_command_policy"],
        )
        self.assertEqual(
            forwarded["payload"]["test_command"],
            "python -m unittest tests.test_test_command_policy",
        )


if __name__ == "__main__":
    unittest.main()
