from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import code_evolution  # noqa: E402
import storage  # noqa: E402


def git_available() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(git_available(), "git executable is required")
class CodeEvolutionCodexAgentTests(unittest.TestCase):
    def _repo(self, tmp: str) -> tuple[pathlib.Path, pathlib.Path]:
        repo = pathlib.Path(tmp) / "repo"
        app = repo / "agentic_trading_swarm_mvp"
        (app / "src").mkdir(parents=True)
        (app / "tests").mkdir()
        (app / "config").mkdir()
        (app / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
        (app / "tests" / "test_smoke.py").write_text(
            "import unittest\n\nclass SmokeTests(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "codex@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "tag", "champion/test"], cwd=repo, check=True)
        return repo, app

    def _settings(self, tmp: str) -> dict:
        return {
            "allow_live_trading": False,
            "codex_repo_agent": {
                "enabled": True,
                "resume_cooldown_seconds": 0,
                "max_resumes_per_cycle": 1,
                "lock_path": str(pathlib.Path(tmp) / "codex.lock"),
            },
            "code_evolution": {
                "enabled": True,
                "git_release_enabled": True,
                "generate_patch_when_missing": True,
                "auto_merge_paper_only": True,
                "promote_candidate_after_canary": True,
                "run_candidate_canary": False,
                "run_full_regression": True,
                "min_priority": 1,
                "release_worktree_dir": str(pathlib.Path(tmp) / "candidate_worktrees"),
                "sandbox_timeout_seconds": 60,
                "allowed_categories": ["runtime_pipeline_integration"],
            },
        }

    def _proposal(self, recommendation_id: str) -> dict:
        return {
            "recommendation_id": recommendation_id,
            "title": "Implement the actual runtime behavior",
            "payload": {
                "action": "propose_code_change",
                "priority": 95,
                "title": "Implement the actual runtime behavior",
                "rationale": "The runtime needs a repository-grounded implementation.",
                "evidence": {"source": "test"},
                "proposed_change": {"goal": "Add ACTUAL_VALUE through the real module."},
                "change_category": "runtime_pipeline_integration",
                "implementation_mode": "runtime_active",
                "expected_files": ["src/guessed_and_missing.py"],
                "tests_to_run": ["pytest tests/test_guessed_and_missing.py"],
            },
        }

    def _completed_agent(self, worktree_root: pathlib.Path, session: str = "codex-thread") -> dict:
        (worktree_root / "src" / "actual_runtime.py").write_text("ACTUAL_VALUE = 2\n", encoding="utf-8")
        (worktree_root / "tests" / "test_actual_runtime.py").write_text(
            "import unittest\nfrom src.actual_runtime import ACTUAL_VALUE\n\n"
            "class ActualRuntimeTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(2, ACTUAL_VALUE)\n",
            encoding="utf-8",
        )
        return {
            "status": "completed",
            "session_id": session,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "event_summary": {"turn_completed": True},
        }

    def test_wrong_preflight_hints_do_not_block_worktree_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, app = self._repo(tmp)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs, old_ledger = code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"

            def agent(**kwargs):
                return self._completed_agent(pathlib.Path(kwargs["worktree_root"]))

            try:
                with mock.patch.object(code_evolution, "run_codex_repo_agent", side_effect=agent) as run:
                    created = code_evolution.process_code_change_recommendation(
                        conn, self._proposal("rec-codex"), self._settings(tmp), root=app
                    )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL = old_runs, old_ledger
                conn.close()

            self.assertEqual("promoted", created[0]["status"])
            self.assertEqual("promoted", row["status"])
            self.assertEqual("codex-thread", row["safety"]["codex_repo_agent"]["session_id"])
            self.assertIn("src/actual_runtime.py", row["changed_files"])
            self.assertTrue((app / "src" / "actual_runtime.py").exists())
            self.assertFalse((app / "src" / "guessed_and_missing.py").exists())
            self.assertEqual(1, run.call_count)

    def test_supplied_unified_diff_is_only_a_codex_hint_when_agent_enabled(self) -> None:
        legacy_diff = """diff --git a/src/existing.py b/src/existing.py
--- a/src/existing.py
+++ b/src/existing.py
@@ -1 +1,2 @@
 VALUE = 1
+LEGACY_PATCH_WAS_APPLIED = True
"""
        with tempfile.TemporaryDirectory() as tmp:
            _repo, app = self._repo(tmp)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs, old_ledger = code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"
            rec = self._proposal("rec-codex-with-diff")
            rec["payload"]["unified_diff"] = legacy_diff

            def agent(**kwargs):
                self.assertIn("LEGACY_PATCH_WAS_APPLIED", kwargs["payload"]["unified_diff"])
                return self._completed_agent(pathlib.Path(kwargs["worktree_root"]), session="thread-with-diff")

            try:
                with mock.patch.object(code_evolution, "run_codex_repo_agent", side_effect=agent) as run:
                    created = code_evolution.process_code_change_recommendation(
                        conn, rec, self._settings(tmp), root=app
                    )
                row = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL = old_runs, old_ledger
                conn.close()

            self.assertEqual("promoted", created[0]["status"])
            self.assertEqual(1, run.call_count)
            self.assertNotIn("LEGACY_PATCH_WAS_APPLIED", (app / "src" / "existing.py").read_text(encoding="utf-8"))
            self.assertIn("src/actual_runtime.py", row["changed_files"])

    def test_paused_thread_resumes_and_promotes_same_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, app = self._repo(tmp)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs, old_ledger = code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"
            calls: list[str | None] = []

            def agent(**kwargs):
                calls.append(kwargs.get("session_id"))
                worktree = pathlib.Path(kwargs["worktree_root"])
                if len(calls) == 1:
                    (worktree / "src" / "partial.py").write_text("PARTIAL = True\n", encoding="utf-8")
                    return {
                        "status": "implementation_paused",
                        "reason": "codex_turn_timeout",
                        "session_id": "thread-resume",
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "high",
                    }
                return self._completed_agent(worktree, session="thread-resume")

            try:
                with mock.patch.object(code_evolution, "run_codex_repo_agent", side_effect=agent):
                    first = code_evolution.process_code_change_recommendation(
                        conn, self._proposal("rec-resume"), self._settings(tmp), root=app
                    )
                    paused = storage.code_evolution_recent(conn)[0]
                    paused_worktree_existed = pathlib.Path(paused["worktree_path"]).exists()
                    evaluated = code_evolution.evaluate_code_evolution(
                        conn, self._settings(tmp), root=app, resume_paused=True
                    )
                final = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL = old_runs, old_ledger
                conn.close()

            self.assertEqual("implementation_paused", first[0]["status"])
            self.assertTrue(paused_worktree_existed)
            self.assertEqual([None, "thread-resume"], calls)
            self.assertTrue(any(item["status"] == "promoted" for item in evaluated))
            self.assertEqual("promoted", final["status"])
            self.assertTrue((app / "src" / "actual_runtime.py").exists())

    def test_stale_runtime_after_promotion_reverts_and_requeues_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, app = self._repo(tmp)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs, old_ledger = code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"
            code_evolution.RUNS_DIR.mkdir(parents=True)

            def agent(**kwargs):
                return self._completed_agent(pathlib.Path(kwargs["worktree_root"]), session="thread-health")

            config = self._settings(tmp)
            config["codex_repo_agent"].update(
                {"post_promotion_health_grace_seconds": 0, "post_promotion_health_loops": 3}
            )
            try:
                with mock.patch.object(code_evolution, "run_codex_repo_agent", side_effect=agent):
                    code_evolution.process_code_change_recommendation(
                        conn, self._proposal("rec-health-fail"), config, root=app
                    )
                old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
                conn.execute(
                    "update code_evolution_proposals set applied_at=? where proposal_id like 'code_%'",
                    (old,),
                )
                for name in ("radar_heartbeat.json", "llm_state_packet.json", "self_improvement_report.md"):
                    path = code_evolution.RUNS_DIR / name
                    path.write_text("stale", encoding="utf-8")
                    timestamp = (dt.datetime.now().timestamp() - 7200)
                    os.utime(path, (timestamp, timestamp))
                evaluated = code_evolution.evaluate_code_evolution(conn, config, root=app)
                row = storage.code_evolution_recent(conn)[0]
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
                ).stdout.strip()
                champion = subprocess.run(
                    ["git", "rev-parse", "champion/latest"], cwd=repo, check=True, capture_output=True, text=True
                ).stdout.strip()
            finally:
                code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL = old_runs, old_ledger
                conn.close()

            self.assertEqual("implementation_paused", row["status"])
            self.assertEqual("thread-health", row["safety"]["codex_repo_agent"]["session_id"])
            self.assertEqual(head, champion)
            self.assertFalse((app / "src" / "actual_runtime.py").exists())
            self.assertTrue(any(item["decision"] == "revert_and_resume" for item in evaluated))

    def test_fresh_runtime_marks_promoted_codex_change_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, app = self._repo(tmp)
            conn = sqlite3.connect(pathlib.Path(tmp) / "radar.sqlite")
            conn.row_factory = sqlite3.Row
            storage.init_db(conn)
            old_runs, old_ledger = code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL
            code_evolution.RUNS_DIR = pathlib.Path(tmp) / "runs"
            code_evolution.LEDGER_JSONL = code_evolution.RUNS_DIR / "evolution_ledger.jsonl"
            code_evolution.RUNS_DIR.mkdir(parents=True)

            def agent(**kwargs):
                return self._completed_agent(pathlib.Path(kwargs["worktree_root"]), session="thread-healthy")

            config = self._settings(tmp)
            config["codex_repo_agent"].update(
                {"post_promotion_health_grace_seconds": 0, "post_promotion_health_loops": 2}
            )
            try:
                with mock.patch.object(code_evolution, "run_codex_repo_agent", side_effect=agent):
                    code_evolution.process_code_change_recommendation(
                        conn, self._proposal("rec-health-pass"), config, root=app
                    )
                old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
                conn.execute("update code_evolution_proposals set applied_at=?", (old,))
                conn.commit()
                for name in ("radar_heartbeat.json", "llm_state_packet.json", "self_improvement_report.md"):
                    (code_evolution.RUNS_DIR / name).write_text("fresh", encoding="utf-8")
                code_evolution.evaluate_code_evolution(conn, config, root=app)
                evaluated = code_evolution.evaluate_code_evolution(conn, config, root=app)
                row = storage.code_evolution_recent(conn)[0]
            finally:
                code_evolution.RUNS_DIR, code_evolution.LEDGER_JSONL = old_runs, old_ledger
                conn.close()

            self.assertEqual("promoted", row["status"])
            self.assertEqual(2, row["probation_loops_observed"])
            self.assertEqual("healthy", row["evaluation"]["post_promotion_health"]["status"])
            self.assertTrue(any(item["decision"] == "healthy" for item in evaluated))


if __name__ == "__main__":
    unittest.main()
