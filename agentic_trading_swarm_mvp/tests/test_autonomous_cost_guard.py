from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import autonomous_cost_guard  # noqa: E402
import codex_repo_agent  # noqa: E402
import cost_router  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402


class AutonomousCostGuardTests(unittest.TestCase):
    def _settings(self, ledger: pathlib.Path, limit: int = 1) -> dict:
        return {
            "autonomous_cost_guard": {
                "enabled": True,
                "daily_paid_attempt_limit": limit,
                "ledger_path": str(ledger),
            }
        }

    def test_autonomous_attempt_limit_is_persisted_and_manual_calls_are_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = pathlib.Path(tmp) / "attempts.sqlite"
            manual = autonomous_cost_guard.claim_autonomous_paid_attempt(
                agent_name="manual",
                operation="offline_test",
            )
            with autonomous_cost_guard.autonomous_paid_scope(
                self._settings(ledger, limit=2),
                source="test",
            ):
                first = autonomous_cost_guard.claim_autonomous_paid_attempt(
                    agent_name="one", operation="first"
                )
                second = autonomous_cost_guard.claim_autonomous_paid_attempt(
                    agent_name="two", operation="second"
                )
                denied = autonomous_cost_guard.claim_autonomous_paid_attempt(
                    agent_name="three", operation="third"
                )

            with autonomous_cost_guard.autonomous_paid_scope(
                self._settings(ledger, limit=2),
                source="new-process-equivalent",
            ):
                persisted = autonomous_cost_guard.autonomous_paid_attempt_status()

        self.assertTrue(manual["allowed"])
        self.assertFalse(manual["guarded"])
        self.assertTrue(first["claimed"])
        self.assertTrue(second["claimed"])
        self.assertFalse(denied["allowed"])
        self.assertEqual("autonomous_daily_paid_attempt_limit", denied["status"])
        self.assertFalse(persisted["allowed"])
        self.assertEqual(2, persisted["used"])

    def test_scope_is_inherited_by_hard_timeout_subprocesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = pathlib.Path(tmp) / "attempts.sqlite"
            script = (
                "import json; from autonomous_cost_guard import claim_autonomous_paid_attempt; "
                "print(json.dumps(claim_autonomous_paid_attempt(agent_name='child', operation='builder')))"
            )
            with autonomous_cost_guard.autonomous_paid_scope(
                self._settings(ledger, limit=1),
                source="test-parent",
            ):
                env = os.environ.copy()
                env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=str(ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                child = json.loads(completed.stdout)
                parent = autonomous_cost_guard.claim_autonomous_paid_attempt(
                    agent_name="parent", operation="second"
                )

        self.assertTrue(child["claimed"])
        self.assertFalse(parent["allowed"])

    def test_cost_router_claims_only_when_a_real_provider_call_is_ready(self) -> None:
        model_cfg = {
            "require_env_to_call_models": True,
            "daily_budget_usd": 100.0,
            "tiers": {
                "fast": {
                    "model": "local/test-model",
                    "api": "litellm",
                    "max_prompt_chars": 12000,
                    "estimated_completion_tokens": 100,
                    "input_cost_per_1m": 1.0,
                    "output_cost_per_1m": 1.0,
                }
            },
            "agents": {"test-agent": {"tier": "fast", "daily_budget_usd": 100.0}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = pathlib.Path(tmp) / "attempts.sqlite"
            with autonomous_cost_guard.autonomous_paid_scope(
                self._settings(ledger, limit=1),
                source="test",
            ):
                with mock.patch.object(cost_router, "load_llm_config", return_value=model_cfg), mock.patch.object(
                    cost_router, "_log"
                ), mock.patch.object(
                    cost_router,
                    "_reserve_model_call",
                    side_effect=[
                        {"allowed": True, "event_id": "event-1", "created_at": "2026-08-07T00:00:00+00:00"},
                        {"allowed": True, "event_id": "event-2", "created_at": "2026-08-07T00:00:01+00:00"},
                    ],
                ), mock.patch.object(
                    cost_router, "_cancel_model_reservation"
                ), mock.patch.dict(
                    os.environ,
                    {
                        "RADAR_USE_LITELLM": "1",
                        "RADAR_MODEL_CREDENTIAL_LOCK": "0",
                        "RADAR_MODELS_DISABLED": "0",
                    },
                    clear=False,
                ), mock.patch.object(
                    cost_router, "_complete_litellm", return_value=("paid", 4, "stop")
                ) as provider:
                    first = cost_router.complete("test-agent", "first", operation="test_call")
                    second = cost_router.complete("test-agent", "second", operation="test_call")

        self.assertTrue(first.status.startswith("model_call:"))
        self.assertIn("autonomous_daily_paid_attempt_budget_exhausted", second.status)
        self.assertEqual(1, provider.call_count)

    def test_codex_cli_entry_points_stop_before_process_start_when_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ledger = root / "attempts.sqlite"
            settings = self._settings(ledger, limit=0)
            settings["codex_repo_agent"] = {
                "enabled": True,
                "chatgpt_account_fallback_enabled": True,
                "lock_path": str(root / "codex.lock"),
                "session_log_dir": str(root / "sessions"),
                "codex_home": str(root / "codex-home"),
            }
            runtime = {"available": True, "command_prefix": ["codex"], "source": "test"}
            with autonomous_cost_guard.autonomous_paid_scope(settings, source="test"):
                with mock.patch.object(codex_repo_agent, "ensure_codex_runtime", return_value=runtime), mock.patch.object(
                    codex_repo_agent, "_execute_with_auth_fallback"
                ) as execute:
                    repo = codex_repo_agent.run_codex_repo_agent(
                        proposal_id="proposal:test",
                        payload={},
                        preflight_hints={},
                        worktree_root=root,
                        settings=settings,
                        runs_dir=root,
                    )
                    structured = codex_repo_agent.run_structured_codex_turn(
                        task_id="task:test",
                        prompt="return json",
                        output_schema={"type": "object"},
                        worktree_root=root,
                        settings=settings,
                        runs_dir=root,
                    )

        self.assertEqual("implementation_paused", repo["status"])
        self.assertEqual("implementation_paused", structured["status"])
        self.assertIn("budget_exhausted", repo["reason"])
        self.assertIn("budget_exhausted", structured["reason"])
        execute.assert_not_called()

    def test_autonomous_coding_is_default_off_with_a_fail_closed_guard(self) -> None:
        self.assertFalse(DEFAULT_SETTINGS["evolution_worker"]["enabled"])
        self.assertTrue(DEFAULT_SETTINGS["autonomous_cost_guard"]["enabled"])
        self.assertEqual(10, DEFAULT_SETTINGS["autonomous_cost_guard"]["daily_paid_attempt_limit"])


if __name__ == "__main__":
    unittest.main()
