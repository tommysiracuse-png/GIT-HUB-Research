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

import codex_repo_agent  # noqa: E402


class CodexRepoAgentTests(unittest.TestCase):
    def _settings(self, tmp: str) -> dict:
        base = pathlib.Path(tmp)
        return {
            "codex_repo_agent": {
                "enabled": True,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "timeout_seconds": 30,
                "runtime_dir": str(base / "runtime"),
                "codex_home": str(base / "home"),
                "session_log_dir": str(base / "logs"),
                "lock_path": str(base / "writer.lock"),
                "auto_install_npm_fallback": False,
            }
        }

    def test_prompt_marks_guessed_paths_and_tests_as_hints(self) -> None:
        prompt = codex_repo_agent.build_implementation_prompt(
            {"title": "Repair adapter", "proposed_change": {"goal": "wire the real adapter"}},
            {"target_files": ["src/not_real.py"], "parsed_tests": [["python", "missing_test.py"]]},
        )

        self.assertIn("non-authoritative hints", prompt.lower())
        self.assertIn("Search the repository", prompt)
        self.assertIn("src/not_real.py", prompt)
        self.assertIn("run focused tests", prompt)
        self.assertIn("Preserve priceable candidate emission", prompt)
        self.assertIn("counterfactual guard-value measurement", prompt)

    def test_default_is_terra_high_with_shared_account_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = codex_repo_agent.codex_repo_agent_config({}, pathlib.Path(tmp))

        self.assertEqual("gpt-5.6-terra", cfg["model"])
        self.assertEqual("high", cfg["reasoning_effort"])
        self.assertEqual(pathlib.Path.home() / ".codex", pathlib.Path(cfg["codex_home"]))
        self.assertTrue(cfg["chatgpt_account_fallback_enabled"])
        self.assertTrue(cfg["fallback_on_api_quota"])

    def test_runtime_resolution_prefers_python_bundled_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = pathlib.Path(tmp) / "codex-bundled.exe"
            executable.write_bytes(b"binary")
            bundled_module = mock.Mock()
            bundled_module.bundled_codex_path.return_value = executable
            cfg = codex_repo_agent.codex_repo_agent_config(self._settings(tmp), pathlib.Path(tmp))
            with mock.patch.object(
                codex_repo_agent.importlib,
                "import_module",
                return_value=bundled_module,
            ) as importer:
                resolved = codex_repo_agent.ensure_codex_runtime(cfg)

        importer.assert_called_once_with("codex_cli_bin")
        self.assertTrue(resolved["available"])
        self.assertEqual("bundled_python_package", resolved["source"])
        self.assertEqual([str(executable.resolve())], resolved["command_prefix"])
        self.assertEqual("openai-codex==0.144.4", resolved["runtime_package"])

    def test_configured_cli_remains_an_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = pathlib.Path(tmp) / "configured-codex.exe"
            executable.write_bytes(b"binary")
            settings = self._settings(tmp)
            settings["codex_repo_agent"]["cli_path"] = str(executable)
            cfg = codex_repo_agent.codex_repo_agent_config(settings, pathlib.Path(tmp))
            with mock.patch.object(codex_repo_agent.importlib, "import_module") as importer:
                resolved = codex_repo_agent.ensure_codex_runtime(cfg)

        self.assertEqual("configured", resolved["source"])
        self.assertEqual(0, importer.call_count)

    def test_new_turn_uses_json_api_key_scope_and_persists_thread(self) -> None:
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}}),
            ]
        )
        completed = subprocess.CompletedProcess(["codex"], 0, stdout=events, stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(codex_repo_agent.subprocess, "run", return_value=completed) as run, mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-secret-value", "OTHER_ENV": "kept"},
            clear=True,
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:one",
                payload={"title": "Implement it", "proposed_change": {"goal": "make a tested change"}},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("thread-123", result["session_id"])
        command = run.call_args.args[0]
        child_env = run.call_args.kwargs["env"]
        self.assertIn("exec", command)
        self.assertIn("--json", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual("test-secret-value", child_env["CODEX_API_KEY"])
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("test-secret-value", run.call_args.kwargs["input"])
        self.assertNotIn("test-secret-value", json.dumps(result))

    def test_resume_uses_persisted_session_id(self) -> None:
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        completed = subprocess.CompletedProcess(["codex"], 0, stdout=events, stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(codex_repo_agent.subprocess, "run", return_value=completed) as run, mock.patch.dict(
            os.environ, {"CODEX_API_KEY": "test-key"}, clear=True
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:resume",
                payload={"title": "Resume"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
                session_id="thread-123",
                failure_context={"host_tests": "failed"},
            )

        command = run.call_args.args[0]
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["resumed"])
        self.assertIn("resume", command)
        self.assertIn("thread-123", command)
        self.assertNotIn("--cd", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("host_tests", run.call_args.kwargs["input"])

    def test_api_quota_retries_same_turn_with_chatgpt_account(self) -> None:
        quota = subprocess.CompletedProcess(
            ["codex"],
            1,
            stdout=json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "stream disconnected before completion: You have no credits remaining. Add credits to continue using the API."
                    },
                }
            ),
            stderr="",
        )
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "account-thread"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8}}),
            ]
        )
        account = subprocess.CompletedProcess(["codex"], 0, stdout=events, stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(
            codex_repo_agent.subprocess, "run", side_effect=[quota, account]
        ) as run, mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:quota-fallback",
                payload={"title": "Continue through quota"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("account-thread", result["session_id"])
        self.assertEqual("chatgpt_account_fallback", result["auth_mode"])
        self.assertTrue(result["api_quota_fallback"])
        self.assertEqual(2, run.call_count)
        self.assertEqual("test-key", run.call_args_list[0].kwargs["env"]["CODEX_API_KEY"])
        self.assertNotIn("CODEX_API_KEY", run.call_args_list[1].kwargs["env"])
        self.assertEqual(
            run.call_args_list[0].kwargs["env"]["CODEX_HOME"],
            run.call_args_list[1].kwargs["env"]["CODEX_HOME"],
        )

    def test_transient_rate_limit_does_not_switch_auth(self) -> None:
        limited = subprocess.CompletedProcess(
            ["codex"],
            1,
            stdout=json.dumps({"type": "error", "message": "429 rate_limit_exceeded; retry later"}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(
            codex_repo_agent.subprocess, "run", return_value=limited
        ) as run, mock.patch.dict(
            os.environ, {"CODEX_API_KEY": "test-key"}, clear=True
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:rate-limit",
                payload={"title": "Do not switch auth"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("failed", result["status"])
        self.assertFalse(result["api_quota_fallback"])
        self.assertEqual("api_key", result["auth_mode"])
        self.assertEqual(1, run.call_count)

    def test_missing_api_key_uses_cached_chatgpt_account(self) -> None:
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "account-only"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        completed = subprocess.CompletedProcess(["codex"], 0, stdout=events, stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(
            codex_repo_agent.subprocess, "run", return_value=completed
        ) as run, mock.patch.dict(os.environ, {}, clear=True):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:account-only",
                payload={"title": "Use cached account"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual("chatgpt_account", result["auth_mode"])
        self.assertNotIn("CODEX_API_KEY", run.call_args.kwargs["env"])

    def test_timeout_with_thread_becomes_resumable_pause(self) -> None:
        partial = json.dumps({"type": "thread.started", "thread_id": "thread-timeout"}) + "\n"
        timeout = subprocess.TimeoutExpired(["codex"], 30, output=partial, stderr="still working")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(codex_repo_agent.subprocess, "run", side_effect=timeout), mock.patch.dict(
            os.environ, {"CODEX_API_KEY": "test-key"}, clear=True
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:timeout",
                payload={"title": "Long repair"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("implementation_paused", result["status"])
        self.assertEqual("thread-timeout", result["session_id"])
        self.assertEqual("codex_turn_timeout", result["reason"])

    def test_api_key_is_scrubbed_from_cli_output_and_event_log(self) -> None:
        secret = "test-secret-value"
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-secret"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": secret}}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        completed = subprocess.CompletedProcess(["codex"], 0, stdout=events, stderr=secret)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(codex_repo_agent.subprocess, "run", return_value=completed), mock.patch.dict(
            os.environ, {"CODEX_API_KEY": secret}, clear=True
        ):
            result = codex_repo_agent.run_codex_repo_agent(
                proposal_id="proposal:secret",
                payload={"title": "Secret scrub"},
                preflight_hints={},
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )
            event_log = pathlib.Path(result["event_log"]).read_text(encoding="utf-8")

        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, event_log)
        self.assertIn("[REDACTED]", event_log)

    def test_single_writer_lock_rejects_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = codex_repo_agent.codex_repo_agent_config(self._settings(tmp), pathlib.Path(tmp))
            with codex_repo_agent.codex_write_lock(cfg, "first") as first:
                with codex_repo_agent.codex_write_lock(cfg, "second") as second:
                    self.assertTrue(first["acquired"])
                    self.assertFalse(second["acquired"])
                    self.assertEqual("codex_writer_busy", second["reason"])

    def test_structured_turn_uses_schema_and_last_message_artifact(self) -> None:
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "strategy-thread-1"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
            ]
        )

        def fake_run(command, **kwargs):
            message_path = pathlib.Path(command[command.index("--output-last-message") + 1])
            message_path.write_text(json.dumps({"decision": "completed"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=events, stderr="")

        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            codex_repo_agent,
            "ensure_codex_runtime",
            return_value={"available": True, "command_prefix": ["codex-test"], "source": "test"},
        ), mock.patch.object(codex_repo_agent.subprocess, "run", side_effect=fake_run) as run, mock.patch.dict(
            os.environ, {"CODEX_API_KEY": "test-key"}, clear=True
        ):
            result = codex_repo_agent.run_structured_codex_turn(
                task_id="strategy:one",
                prompt="Analyze only",
                output_schema=schema,
                worktree_root=pathlib.Path(tmp),
                settings=self._settings(tmp),
                runs_dir=pathlib.Path(tmp),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual({"decision": "completed"}, result["decision"])
        command = run.call_args.args[0]
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])
        self.assertEqual("replace", run.call_args.kwargs["errors"])

    def test_dead_pid_writer_lock_is_reclaimed_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = codex_repo_agent.codex_repo_agent_config(self._settings(tmp), pathlib.Path(tmp))
            lock_path = pathlib.Path(cfg["lock_path"])
            lock_path.write_text(json.dumps({"owner": "dead", "pid": 99999999}), encoding="utf-8")
            old = lock_path.stat().st_mtime - 10
            os.utime(lock_path, (old, old))
            with codex_repo_agent.codex_write_lock(cfg, "replacement") as lock:
                self.assertTrue(lock["acquired"])


if __name__ == "__main__":
    unittest.main()
