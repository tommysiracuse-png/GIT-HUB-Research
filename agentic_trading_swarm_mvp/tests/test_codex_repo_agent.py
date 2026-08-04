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
        self.assertIn("workspace-write", command)
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
        self.assertIn("host_tests", run.call_args.kwargs["input"])

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

    def test_single_writer_lock_rejects_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = codex_repo_agent.codex_repo_agent_config(self._settings(tmp), pathlib.Path(tmp))
            with codex_repo_agent.codex_write_lock(cfg, "first") as first:
                with codex_repo_agent.codex_write_lock(cfg, "second") as second:
                    self.assertTrue(first["acquired"])
                    self.assertFalse(second["acquired"])
                    self.assertEqual("codex_writer_busy", second["reason"])


if __name__ == "__main__":
    unittest.main()
