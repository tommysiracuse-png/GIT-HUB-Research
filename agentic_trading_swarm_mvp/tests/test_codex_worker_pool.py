from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import codex_coordination
import codex_worker_pool
import storage


class CodexWorkerPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tempdir.name)
        self.coord_path = self.root / "coordination.sqlite"
        self.radar_path = self.root / "radar.sqlite"
        self.settings = {
            "allow_live_trading": False,
            "codex_worker_pool": {
                "enabled": True,
                "coordination_db": str(self.coord_path),
                "max_workers": 3,
                "max_verifiers": 2,
                "task_lease_seconds": 60,
                "verification_timeout_seconds": 60,
            },
            "codex_repo_agent": {"enabled": True},
            "code_evolution": {"enabled": True, "run_full_regression": True},
        }
        with closing(storage.connect(self.radar_path)) as conn:
            storage.init_db(conn)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _radar_connect(self):
        return storage.connect(self.radar_path)

    def test_worker_settings_defer_only_full_regression(self) -> None:
        configured = codex_worker_pool._worker_settings(self.settings, "strategy-codex")
        self.assertTrue(configured["_codex_worker_execute"])
        self.assertEqual("strategy-codex", configured["_codex_worker_id"])
        self.assertFalse(configured["code_evolution"]["run_full_regression"])
        self.assertTrue(configured["codex_repo_agent"]["parallel_sessions_enabled"])
        self.assertTrue(self.settings["code_evolution"]["run_full_regression"])

    def test_sync_migrates_paused_proposal_to_preferred_lane(self) -> None:
        with closing(self._radar_connect()) as radar:
            storage.add_code_evolution_proposal(
                radar, "proposal-1", None, "strategy_implementation_owner", "openai/test", "standard", None,
                "Promote strategy", "strategy_lab_promotion", 96,
                {"agent_name": "strategy_implementation_owner", "title": "Promote strategy"}, {},
                status="implementation_paused",
            )
            with closing(codex_coordination.connect(self.coord_path)) as coord:
                result = codex_worker_pool.sync_available_work(radar, coord, self.settings)
                claimed = codex_coordination.claim_task(
                    coord, "strategy-codex", preferred_lane="strategy", pid=123, lease_seconds=60,
                    pid_alive=lambda _pid: True,
                )
        self.assertGreaterEqual(result["queued_or_refreshed"], 1)
        self.assertEqual("code_evolution_proposal", claimed["source_kind"])
        self.assertEqual("strategy", claimed["lane"])

    def test_worker_promotion_enqueues_async_verification(self) -> None:
        with closing(codex_coordination.connect(self.coord_path)) as coord:
            task = codex_coordination.enqueue_task(
                coord, "code_evolution_proposal", "proposal-2", lane="general", priority=90,
                payload={"proposal_id": "proposal-2"},
            )
        promoted = [{
            "artifact_type": "code_evolution", "proposal_id": "proposal-2",
            "status": "promoted_pending_verification",
        }]
        with (
            mock.patch.object(codex_worker_pool, "_dispatch", return_value=promoted),
            mock.patch.object(codex_worker_pool, "connect", side_effect=self._radar_connect),
        ):
            result = codex_worker_pool.run_worker_once(
                {"worker_id": "system-codex", "preferred_lanes": ["general"]}, self.settings
            )
        with closing(codex_coordination.connect(self.coord_path)) as coord:
            saved = coord.execute("select status from codex_tasks where task_id=?", (task["task_id"],)).fetchone()
            verification = coord.execute(
                "select status from codex_verification_jobs where task_id=?", (task["task_id"],)
            ).fetchone()
        self.assertEqual("promoted_pending_verification", result["status"])
        self.assertEqual("promoted_pending_verification", saved[0])
        self.assertEqual("queued", verification[0])

    def test_failed_async_verification_keeps_code_active_and_requeues_same_task(self) -> None:
        worktree = self.root / "candidate"
        app_worktree = worktree / "agentic_trading_swarm_mvp"
        app_worktree.mkdir(parents=True)
        with closing(self._radar_connect()) as radar:
            storage.add_code_evolution_proposal(
                radar, "proposal-3", None, "test", "openai/test", "standard", None,
                "Repair me", "runtime_pipeline_integration", 90, {}, {},
                status="promoted_pending_verification",
            )
            storage.update_code_evolution_proposal(
                radar, "proposal-3", status="promoted_pending_verification", parent_commit="a" * 40,
                candidate_commit="b" * 40, branch_name="evolution/proposal-3", worktree_path=str(worktree),
                evaluation={"release": {"app_worktree_path": str(app_worktree), "worktree_path": str(worktree)}},
            )
        with closing(codex_coordination.connect(self.coord_path)) as coord:
            task = codex_coordination.enqueue_task(
                coord, "code_evolution_proposal", "proposal-3", lane="general", priority=90,
                payload={"proposal_id": "proposal-3"},
            )
            codex_coordination.complete_task(coord, task["task_id"], status="promoted_pending_verification")
            codex_coordination.enqueue_verification_job(
                coord, task["task_id"], payload={"proposal_id": "proposal-3"},
            )
        with (
            mock.patch.object(codex_worker_pool, "connect", side_effect=self._radar_connect),
            mock.patch.object(codex_worker_pool, "_run_full_regression", return_value={"passed": False, "returncode": 1}),
            mock.patch.object(
                codex_worker_pool, "_prepare_repair_worktree",
                return_value={"prepared": True, "parent_commit": "c" * 40},
            ),
        ):
            result = codex_worker_pool.run_verifier_once(0, self.settings)
        with closing(self._radar_connect()) as radar:
            proposal = storage.get_code_evolution_proposal(radar, "proposal-3")
        with closing(codex_coordination.connect(self.coord_path)) as coord:
            queued = coord.execute("select status from codex_tasks where task_id=?", (task["task_id"],)).fetchone()[0]
        self.assertEqual("repairing_post_promotion", result["status"])
        self.assertEqual("implementation_paused", proposal["status"])
        self.assertIsNone(proposal["candidate_commit"])
        self.assertEqual("requeued", queued)

    def test_runtime_sync_pushes_newer_main_while_tagging_verified_commit(self) -> None:
        commands = []

        def fake_git(args, _root, timeout=0):
            commands.append((list(args), timeout))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with (
            mock.patch.object(codex_worker_pool, "current_commit", return_value="newer-main"),
            mock.patch.object(codex_worker_pool, "run_git", side_effect=fake_git),
            mock.patch.object(codex_worker_pool, "update_champion_latest", return_value={"ok": True}),
        ):
            result = codex_worker_pool._sync_verified_runtime(self.root, "verified-candidate")

        self.assertTrue(result["ok"])
        self.assertIn(
            (["push", "origin", "newer-main:refs/heads/main"], 120),
            commands,
        )
        self.assertEqual("verified-candidate", result["verified_commit"])

    def test_quick_owner_task_does_not_idle_worker_behind_longest_session(self) -> None:
        quick = {"worker_id": "market-codex", "status": "not_due", "elapsed_seconds": 0.02}
        coding = {
            "worker_id": "market-codex", "status": "promoted_pending_verification",
            "elapsed_seconds": 12.0, "proposal_id": "adapter-proposal",
        }
        with mock.patch.object(
            codex_worker_pool, "_run_one_worker_task", side_effect=[quick, coding]
        ) as run_task:
            result = codex_worker_pool.run_worker_once(
                {"worker_id": "market-codex", "preferred_lanes": ["adapter"]}, self.settings
            )

        self.assertEqual(2, run_task.call_count)
        self.assertEqual("adapter-proposal", result["proposal_id"])
        self.assertEqual(2, result["tasks_processed_this_turn"])
        self.assertEqual(1, result["quick_handoffs"])


if __name__ == "__main__":
    unittest.main()
