from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import autonomous_builder
from cost_router import ModelResult


class AutonomousBuilderTests(unittest.TestCase):
    def _patch_report_paths(self, tmp: str):
        old_json = autonomous_builder.REPORT_JSON
        old_md = autonomous_builder.REPORT_MD
        old_marker = autonomous_builder.MARKER
        old_lock = autonomous_builder.LOCK
        autonomous_builder.REPORT_JSON = pathlib.Path(tmp) / "autonomous_builder_report.json"
        autonomous_builder.REPORT_MD = pathlib.Path(tmp) / "autonomous_builder_report.md"
        autonomous_builder.MARKER = pathlib.Path(tmp) / "autonomous_builder_last_run.txt"
        autonomous_builder.LOCK = pathlib.Path(tmp) / "autonomous_builder.lock"
        return old_json, old_md, old_marker, old_lock

    def _restore_report_paths(self, old_paths) -> None:
        (
            autonomous_builder.REPORT_JSON,
            autonomous_builder.REPORT_MD,
            autonomous_builder.MARKER,
            autonomous_builder.LOCK,
        ) = old_paths

    def test_model_unavailable_does_not_attempt_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_paths = self._patch_report_paths(tmp)
            try:
                result = ModelResult(
                    text="{}",
                    model_name="openai/gpt-5.6-sol",
                    model_tier="frontier",
                    prompt_tokens=10,
                    completion_tokens=1,
                    estimated_cost_usd=0.0,
                    status="fallback_error:429 insufficient_quota",
                )
                with mock.patch.object(autonomous_builder, "complete", return_value=result):
                    with mock.patch.object(autonomous_builder, "process_code_change_recommendation") as process:
                        report = autonomous_builder._run_with_conn(
                            object(),
                            {"autonomous_builder": {"use_hard_model_timeout": False}},
                            force=True,
                        )

                self.assertEqual(report["status"], "model_unavailable")
                process.assert_not_called()
            finally:
                self._restore_report_paths(old_paths)

    def test_default_builder_hands_plan_to_code_evolution_without_direct_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_paths = self._patch_report_paths(tmp)
            try:
                plan_text = json.dumps(
                    {
                        "plan": "Wire a tiny state-packet marker.",
                        "title": "Autonomous marker",
                        "priority": 95,
                        "expected_behavior_change": "Adds a runtime-visible marker.",
                        "evidence": "unit test evidence",
                        "implementation_notes": ["Patch llm_bridge", "Run focused test"],
                        "expected_files": ["src/llm_bridge.py"],
                        "tests_to_run": ["python -m unittest tests/test_frontier_model_policy.py"],
                        "change_category": "llm_prompt_state_packet",
                        "implementation_mode": "runtime_active",
                        "rollback_criteria": "Revert if tests fail.",
                        "frontier_escalation_reason": "unit test",
                    }
                )
                plan_result = ModelResult(
                    text=plan_text,
                    model_name="openai/gpt-5.6-sol",
                    model_tier="frontier",
                    prompt_tokens=10,
                    completion_tokens=20,
                    estimated_cost_usd=0.01,
                    status="model_call:responses",
                )
                with mock.patch.object(autonomous_builder, "complete", return_value=plan_result) as complete:
                    with mock.patch.object(
                        autonomous_builder,
                        "process_code_change_recommendation",
                        return_value=[{"artifact_type": "code_evolution", "proposal_id": "p1", "status": "promoted"}],
                    ) as process:
                        with mock.patch.object(autonomous_builder, "write_code_evolution_reports"):
                            report = autonomous_builder._run_with_conn(
                                object(),
                                {"autonomous_builder": {"use_hard_model_timeout": False}},
                                force=True,
                            )

                self.assertEqual(report["status"], "handed_off_to_code_evolution")
                self.assertEqual(complete.call_count, 1)
                self.assertEqual(complete.call_args.kwargs["operation"], "autonomous_builder_plan")
                payload = process.call_args.args[1]["payload"]
                self.assertEqual(payload["agent_name"], "autonomous_builder")
                self.assertEqual(payload["autonomous_builder_strategy"], "code_evolution_handoff")
                self.assertNotIn("unified_diff", payload)
                self.assertNotIn("unified_diff", payload["code_change"])
                self.assertEqual(payload["evidence"]["summary"], "unit test evidence")
            finally:
                self._restore_report_paths(old_paths)

    def test_valid_builder_patch_flows_to_code_evolution_when_direct_diff_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_paths = self._patch_report_paths(tmp)
            try:
                patch = """diff --git a/src/llm_bridge.py b/src/llm_bridge.py
--- a/src/llm_bridge.py
+++ b/src/llm_bridge.py
@@ -1 +1,2 @@
 # bridge
+AUTONOMOUS_BUILDER_TEST = True
"""
                plan_text = json.dumps(
                    {
                        "plan": "Wire a tiny state-packet marker.",
                        "title": "Autonomous marker",
                        "priority": 95,
                        "expected_behavior_change": "Adds a runtime-visible marker.",
                        "evidence": {"source": "unit test"},
                        "implementation_notes": ["Patch llm_bridge", "Run focused test"],
                        "expected_files": ["src/llm_bridge.py"],
                        "tests_to_run": ["python -m unittest tests/test_frontier_model_policy.py"],
                        "change_category": "llm_prompt_state_packet",
                        "implementation_mode": "runtime_active",
                        "rollback_criteria": "Revert if tests fail.",
                        "frontier_escalation_reason": "unit test",
                    }
                )
                impl_text = patch
                plan_result = ModelResult(
                    text=plan_text,
                    model_name="openai/gpt-5.6-sol",
                    model_tier="frontier",
                    prompt_tokens=10,
                    completion_tokens=20,
                    estimated_cost_usd=0.01,
                    status="model_call:responses",
                )
                impl_result = ModelResult(
                    text=impl_text,
                    model_name="openai/gpt-5.6-sol",
                    model_tier="frontier",
                    prompt_tokens=10,
                    completion_tokens=20,
                    estimated_cost_usd=0.01,
                    status="model_call:responses",
                )
                with mock.patch.object(autonomous_builder, "complete", side_effect=[plan_result, impl_result]) as complete:
                    with mock.patch.object(
                        autonomous_builder,
                        "process_code_change_recommendation",
                        return_value=[{"artifact_type": "code_evolution", "proposal_id": "p1", "status": "merged_probation"}],
                    ) as process:
                        with mock.patch.object(autonomous_builder, "write_code_evolution_reports"):
                            report = autonomous_builder._run_with_conn(
                                object(),
                                {
                                    "autonomous_builder": {
                                        "implementation_strategy": "direct_diff",
                                        "use_hard_model_timeout": False,
                                    }
                                },
                                force=True,
                            )

                self.assertEqual(report["status"], "attempted_patch")
                self.assertEqual(complete.call_args_list[0].kwargs["operation"], "autonomous_builder_plan")
                self.assertEqual(complete.call_args_list[1].kwargs["operation"], "autonomous_builder_implementation")
                payload = process.call_args.args[1]["payload"]
                self.assertEqual(payload["action"], "propose_code_change")
                self.assertEqual(payload["agent_name"], "autonomous_builder")
                self.assertIn("autonomous_plan", payload)
                self.assertIn("unified_diff", payload)
                self.assertEqual(payload["code_change"]["unified_diff"], patch.strip())
            finally:
                self._restore_report_paths(old_paths)

    def test_existing_lock_skips_overlapping_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_paths = self._patch_report_paths(tmp)
            try:
                autonomous_builder.LOCK.write_text("2026-07-10T18:00:00+00:00\n", encoding="utf-8")
                with mock.patch.object(autonomous_builder, "complete") as complete:
                    report = autonomous_builder._run_with_conn(
                        object(),
                        {"autonomous_builder": {"lock_stale_minutes": 999999}},
                        force=True,
                    )
                self.assertEqual(report["status"], "already_running")
                complete.assert_not_called()
            finally:
                self._restore_report_paths(old_paths)

    def test_dead_owner_lock_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_paths = self._patch_report_paths(tmp)
            try:
                autonomous_builder.LOCK.write_text(
                    json.dumps(
                        {
                            "run_id": "dead",
                            "pid": 99999999,
                            "process_start_time": "old",
                            "acquired_at": autonomous_builder._utc_now(),
                            "heartbeat_at": autonomous_builder._utc_now(),
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(autonomous_builder._lock_active({}))
                run_id = autonomous_builder._acquire_lock({})
                self.assertTrue(run_id)
                autonomous_builder._release_lock(run_id)
                self.assertFalse(autonomous_builder.LOCK.exists())
            finally:
                self._restore_report_paths(old_paths)


if __name__ == "__main__":
    unittest.main()

