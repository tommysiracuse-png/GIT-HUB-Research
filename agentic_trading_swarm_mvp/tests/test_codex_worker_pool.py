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
        peer_context = {"active_peer_work": [{"work_scope": "adapter:one"}]}
        configured = codex_worker_pool._worker_settings(
            self.settings, "strategy-codex", peer_context
        )
        self.assertFalse(configured["_codex_worker_execute"])
        self.assertEqual("strategy-codex", configured["_codex_worker_id"])
        self.assertFalse(configured["code_evolution"]["run_full_regression"])
        self.assertTrue(configured["codex_repo_agent"]["parallel_sessions_enabled"])
        self.assertEqual(peer_context, configured["codex_repo_agent"]["coordination_context"])
        self.assertTrue(self.settings["code_evolution"]["run_full_regression"])
        proposal_worker = codex_worker_pool._worker_settings(
            self.settings, "strategy-codex", peer_context, execute_code_changes=True
        )
        self.assertTrue(proposal_worker["_codex_worker_execute"])

    def test_semantic_work_identity_collapses_action_word_variants(self) -> None:
        base = {
            "category": "paper_scoring_logic",
            "payload": {"market_key": "frontier_crypto_venue_map"},
        }
        fingerprints = {
            codex_worker_pool._proposal_work_identity(
                {**base, "title": title}
            )[0]
            for title in (
                "Gate cost-swallowed frontier paper fills",
                "Stop cost-swallowed frontier paper fills",
                "Shadow cost-negative frontier fills",
            )
        }
        self.assertEqual(1, len(fingerprints))

    def test_adapter_specs_keep_distinct_work_identities(self) -> None:
        first = codex_worker_pool._proposal_work_identity({
            "category": "public_data_adapter",
            "title": "Implement venue A",
            "payload": {"adapter_spec_id": 101},
        })
        second = codex_worker_pool._proposal_work_identity({
            "category": "public_data_adapter",
            "title": "Implement venue B",
            "payload": {"adapter_spec_id": 102},
        })
        self.assertNotEqual(first[0], second[0])

    def test_explicit_revision_is_the_only_new_identity_for_same_topic(self) -> None:
        base = {
            "category": "paper_scoring_logic",
            "title": "Repair OKX basis decay",
            "payload": {"_recommendation_topic_key": "okx-basis-decay"},
        }
        first = codex_worker_pool._proposal_work_identity(base)
        repeated = codex_worker_pool._proposal_work_identity({**base, "title": "Quarantine OKX basis decay"})
        revised = codex_worker_pool._proposal_work_identity(
            {**base, "payload": {"_recommendation_topic_key": "okx-basis-decay", "work_revision": 2}}
        )

        self.assertEqual(first[0], repeated[0])
        self.assertNotEqual(first[0], revised[0])

    def test_strategy_owner_identity_exists_before_proposal_is_linked_back(self) -> None:
        identity = codex_worker_pool._proposal_work_identity(
            {
                "category": "strategy_lab_promotion",
                "title": "Implement a strategy",
                "payload": {"evidence": {"strategy_owner_task_id": "strategy-task-123"}},
            }
        )

        self.assertEqual("strategy_owner:strategy-task-123:revision:1", identity[1])

    def test_registry_backfill_prefers_promoted_work_and_closes_only_retry_duplicates(self) -> None:
        with closing(self._radar_connect()) as radar:
            for proposal_id, status in (
                ("proposal-old-promoted", "promoted"),
                ("proposal-paused-duplicate", "implementation_paused"),
                ("proposal-historical-failure", "discarded_test_failure"),
            ):
                storage.add_code_evolution_proposal(
                    radar, proposal_id, None, "red_team", "openai/test", "standard", None,
                    "Paper-quarantine OKX basis mean-reversion decay", "paper_scoring_logic", 94,
                    {"title": "Paper-quarantine OKX basis mean-reversion decay", "market_key": "OKX|perp_funding_basis"},
                    {}, status=status,
                )
            summary = codex_worker_pool.backfill_code_evolution_work_registry(radar)
            rows = {
                row["proposal_id"]: row
                for row in storage.code_evolution_recent(radar, limit=10)
            }
            registry = radar.execute("select * from code_evolution_work_registry").fetchone()

        self.assertEqual("proposal-old-promoted", registry["canonical_proposal_id"])
        self.assertEqual("superseded_duplicate", rows["proposal-paused-duplicate"]["status"])
        self.assertEqual("discarded_test_failure", rows["proposal-historical-failure"]["status"])
        self.assertEqual("proposal-old-promoted", rows["proposal-paused-duplicate"]["canonical_proposal_id"])
        self.assertEqual(1, summary["duplicates_superseded"])

    def test_legacy_adapter_owner_task_receives_canonical_work_identity(self) -> None:
        with closing(self._radar_connect()) as radar:
            storage.add_adapter_spec(
                radar,
                "recommendation-legacy-adapter",
                "new_global_market",
                91,
                "Implement a new global market adapter",
                {"venue": "TEST_VENUE"},
                {"source": "public_docs"},
            )
            adapter_id = radar.execute(
                "select id from adapter_specs where source_recommendation_id=?",
                ("recommendation-legacy-adapter",),
            ).fetchone()[0]
            with closing(codex_coordination.connect(self.coord_path)) as coord:
                legacy = codex_coordination.enqueue_task(
                    coord,
                    "adapter_owner_turn",
                    str(adapter_id),
                    lane="adapter",
                    priority=91,
                    payload={"title": "Implement a new global market adapter"},
                )

                updated = codex_worker_pool._backfill_work_identities(radar, coord)
                migrated = coord.execute(
                    "select work_fingerprint,work_scope from codex_tasks where task_id=?",
                    (legacy["task_id"],),
                ).fetchone()

        self.assertEqual(1, updated)
        self.assertEqual(f"adapter_spec:{adapter_id}", migrated["work_scope"])
        self.assertTrue(migrated["work_fingerprint"])

    def test_sync_supersedes_duplicate_code_proposals_before_claim(self) -> None:
        with closing(self._radar_connect()) as radar:
            for proposal_id, title in (
                ("proposal-cost-a", "Gate cost-swallowed frontier paper fills"),
                ("proposal-cost-b", "Shadow cost-negative frontier fills"),
            ):
                storage.add_code_evolution_proposal(
                    radar, proposal_id, None, "red_team", "openai/test", "standard", None,
                    title, "paper_scoring_logic", 94,
                    {"title": title, "market_key": "frontier_crypto_venue_map"}, {},
                    status="proposed",
                )
            with closing(codex_coordination.connect(self.coord_path)) as coord:
                result = codex_worker_pool.sync_available_work(radar, coord, self.settings)
                claim = codex_coordination.claim_task(
                    coord, "system-codex", preferred_lane="general"
                )
                second_claim = codex_coordination.claim_task(
                    coord, "market-codex", preferred_lane="general"
                )
                statuses = dict(coord.execute(
                    "select source_id,status from codex_tasks where source_id like 'proposal-cost-%'"
                ).fetchall())
            proposal_statuses = {
                proposal_id: storage.get_code_evolution_proposal(radar, proposal_id)["status"]
                for proposal_id in ("proposal-cost-a", "proposal-cost-b")
            }

        self.assertEqual(1, result["work_registry_backfill"]["duplicates_superseded"])
        self.assertIsNotNone(claim)
        self.assertIsNone(second_claim)
        self.assertEqual(1, len(statuses))
        self.assertEqual(0, list(statuses.values()).count("superseded_duplicate"))
        self.assertEqual(1, list(proposal_statuses.values()).count("superseded_duplicate"))

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

    def test_worker_peer_context_does_not_rehash_the_stored_proposal_id(self) -> None:
        with closing(self._radar_connect()) as radar:
            storage.add_code_evolution_proposal(
                radar, "stable-proposal-id", "source-rec", "test", "openai/test", "standard", None,
                "Stable queued work", "runtime_pipeline_integration", 90,
                {"title": "Stable queued work"}, {}, status="proposed",
            )
            task = {
                "source_id": "stable-proposal-id",
                "payload": {"proposal_id": "stable-proposal-id"},
            }
            worker_settings = {
                "codex_repo_agent": {
                    "coordination_context": {"active_peer_work": [{"work_scope": "peer-work"}]}
                }
            }
            with mock.patch.object(codex_worker_pool, "process_code_change_recommendation", return_value=[]) as process:
                codex_worker_pool._run_code_proposal(radar, task, worker_settings)

        submitted = process.call_args.args[1]
        self.assertEqual("stable-proposal-id", submitted["proposal_id"])
        self.assertIn("coordination_context", submitted["payload"])

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

    def test_exact_main_commit_repairs_interrupted_proposal_status_and_queues_verification(self) -> None:
        proposal_id = "code_evolution:interrupted"
        with closing(self._radar_connect()) as radar:
            storage.add_code_evolution_proposal(
                radar, proposal_id, None, "strategy_implementation_owner", "openai/test", "frontier", None,
                "Recovered strategy code", "strategy_lab_promotion", 95,
                {"agent_name": "strategy_implementation_owner"}, {}, status="proposed",
            )
            with (
                closing(codex_coordination.connect(self.coord_path)) as coord,
                mock.patch.object(codex_worker_pool, "repo_root", return_value=self.root),
                mock.patch.object(codex_worker_pool, "run_git") as run_git,
            ):
                run_git.side_effect = [
                    {
                        "returncode": 0,
                        "stdout": f"verified-sha\tAutonomous candidate {proposal_id}\n",
                        "stderr": "",
                    },
                    {"returncode": 0, "stdout": "parent-sha\n", "stderr": ""},
                ]
                result = codex_worker_pool._reconcile_promoted_commits(radar, coord, self.settings)
                task = coord.execute(
                    "select task_id,status from codex_tasks where source_kind='code_evolution_proposal' and source_id=?",
                    (proposal_id,),
                ).fetchone()
                verification = coord.execute(
                    "select status from codex_verification_jobs where task_id=?", (task["task_id"],)
                ).fetchone()
            proposal = storage.get_code_evolution_proposal(radar, proposal_id)

        self.assertEqual(1, result["reconciled"])
        self.assertEqual("promoted_pending_verification", proposal["status"])
        self.assertEqual("verified-sha", proposal["candidate_commit"])
        self.assertEqual("promoted_pending_verification", task["status"])
        self.assertEqual("queued", verification["status"])


if __name__ == "__main__":
    unittest.main()
