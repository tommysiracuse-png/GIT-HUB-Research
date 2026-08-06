from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import codex_coordination as coordination


class CodexCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tempdir.name) / "coordination.sqlite"
        self.conn = coordination.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_schema_uses_separate_wal_database_and_default_metadata(self) -> None:
        self.assertEqual("wal", self.conn.execute("pragma journal_mode").fetchone()[0])
        self.assertIn("schema_version", self.conn.execute(
            "select key from coordination_metadata"
        ).fetchone()[0])

    def test_enqueue_is_idempotent_by_source_and_preserves_live_claim(self) -> None:
        first = coordination.enqueue_task(
            self.conn, "adapter_spec", 7, lane="adapter", priority=2, payload={"version": 1}
        )
        claimed = coordination.claim_task(
            self.conn, "adapter-1", preferred_lane="adapter", pid=os.getpid()
        )
        second = coordination.enqueue_task(
            self.conn, "adapter_spec", 7, lane="adapter", priority=9, payload={"version": 2}
        )
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual("claimed", second["status"])
        self.assertEqual(9, second["priority"])
        self.assertEqual({"version": 2}, second["payload"])
        self.assertEqual(claimed["claim_token"], second["claim_token"])

    def test_equivalent_sources_share_one_canonical_task(self) -> None:
        first = coordination.enqueue_task(
            self.conn,
            "proposal",
            "one",
            lane="general",
            priority=90,
            payload={"title": "Gate cost-swallowed frontier fills"},
            work_fingerprint="same-work",
            work_scope="frontier:net-edge",
        )
        duplicate = coordination.enqueue_task(
            self.conn,
            "proposal",
            "two",
            lane="general",
            priority=95,
            payload={"title": "Shadow cost-negative frontier fills"},
            work_fingerprint="same-work",
            work_scope="frontier:net-edge",
        )

        self.assertEqual("queued", first["status"])
        self.assertEqual("superseded_duplicate", duplicate["status"])
        self.assertEqual(first["task_id"], duplicate["canonical_task_id"])
        claim = coordination.claim_task(self.conn, "worker-a", preferred_lane="general")
        self.assertEqual(first["task_id"], claim["task_id"])
        self.assertIsNone(coordination.claim_task(self.conn, "worker-b", preferred_lane="general"))

    def test_verified_work_suppresses_a_future_equivalent_task(self) -> None:
        first = coordination.enqueue_task(
            self.conn,
            "proposal",
            "done",
            work_fingerprint="completed-work",
            work_scope="adapter:123",
        )
        coordination.complete_task(self.conn, first["task_id"], status="verified")
        duplicate = coordination.enqueue_task(
            self.conn,
            "proposal",
            "new-source",
            work_fingerprint="completed-work",
            work_scope="adapter:123",
        )
        self.assertEqual("superseded_duplicate", duplicate["status"])
        self.assertEqual(first["task_id"], duplicate["canonical_task_id"])

    def test_different_work_fingerprints_remain_independently_claimable(self) -> None:
        first = coordination.enqueue_task(
            self.conn, "proposal", "venue-a", work_fingerprint="adapter-a", work_scope="adapter:a"
        )
        second = coordination.enqueue_task(
            self.conn, "proposal", "venue-b", work_fingerprint="adapter-b", work_scope="adapter:b"
        )
        claimed = {
            coordination.claim_task(self.conn, "worker-a", preferred_lane="general")["task_id"],
            coordination.claim_task(self.conn, "worker-b", preferred_lane="general")["task_id"],
        }
        self.assertEqual({first["task_id"], second["task_id"]}, claimed)

    def test_peer_context_reports_active_and_recent_work(self) -> None:
        active = coordination.enqueue_task(
            self.conn,
            "proposal",
            "active",
            payload={"title": "Active adapter"},
            work_fingerprint="active-fingerprint",
            work_scope="adapter:active",
        )
        coordination.claim_task(self.conn, "worker-a", preferred_lane="general")
        done = coordination.enqueue_task(
            self.conn,
            "proposal",
            "done",
            payload={"title": "Completed strategy"},
            work_fingerprint="done-fingerprint",
            work_scope="strategy:done",
        )
        coordination.complete_task(self.conn, done["task_id"], status="verified")

        context = coordination.peer_work_context(self.conn)
        self.assertEqual(active["task_id"], context["active_peer_work"][0]["task_id"])
        self.assertEqual(done["task_id"], context["recent_completed_work"][0]["task_id"])

    def test_preferred_lane_then_work_stealing(self) -> None:
        general = coordination.enqueue_task(self.conn, "proposal", "general", lane="general", priority=99)
        strategy = coordination.enqueue_task(self.conn, "proposal", "strategy", lane="strategy", priority=1)
        preferred = coordination.claim_task(self.conn, "strategy-worker", preferred_lane="strategy")
        self.assertEqual(strategy["task_id"], preferred["task_id"])
        stolen = coordination.claim_task(self.conn, "strategy-worker-2", preferred_lane="strategy")
        self.assertEqual(general["task_id"], stolen["task_id"])

    def test_concurrent_claims_never_duplicate_ownership(self) -> None:
        task_count = 24
        for index in range(task_count):
            coordination.enqueue_task(self.conn, "proposal", f"p-{index}", lane="general", priority=index)
        self.conn.close()
        claimed_ids: list[str] = []
        claimed_lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            conn = coordination.connect(self.db_path)
            try:
                barrier.wait()
                while True:
                    task = coordination.claim_task(conn, f"worker-{index}", preferred_lane="general", pid=os.getpid())
                    if task is None:
                        return
                    with claimed_lock:
                        claimed_ids.append(task["task_id"])
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(task_count, len(claimed_ids))
        self.assertEqual(task_count, len(set(claimed_ids)))
        self.conn = coordination.connect(self.db_path)

    def test_dead_expired_lease_is_reclaimed_but_live_owner_is_not(self) -> None:
        dead = coordination.enqueue_task(self.conn, "proposal", "dead", lane="general", priority=2)
        live = coordination.enqueue_task(self.conn, "proposal", "live", lane="general", priority=1)
        first = coordination.claim_task(self.conn, "dead-worker", preferred_lane="general", pid=999_999, lease_seconds=60)
        second = coordination.claim_task(self.conn, "live-worker", preferred_lane="general", pid=123, lease_seconds=60)
        self.assertEqual(dead["task_id"], first["task_id"])
        self.assertEqual(live["task_id"], second["task_id"])
        self.conn.execute("update codex_tasks set lease_expires_at=?", ("2000-01-01T00:00:00+00:00",))
        self.conn.commit()
        reclaimed = coordination.reclaim_stale_tasks(self.conn, pid_alive=lambda pid: pid == 123)
        self.assertEqual(1, reclaimed)
        self.assertEqual("requeued", self.conn.execute(
            "select status from codex_tasks where task_id=?", (dead["task_id"],)
        ).fetchone()[0])
        self.assertEqual("claimed", self.conn.execute(
            "select status from codex_tasks where task_id=?", (live["task_id"],)
        ).fetchone()[0])

    def test_worker_heartbeat_complete_and_requeue_transitions(self) -> None:
        worker = coordination.heartbeat_worker(
            self.conn, "strategy-worker", preferred_lane="strategy", pid=os.getpid(), state="coding"
        )
        self.assertEqual("coding", worker["state"])
        task = coordination.enqueue_task(self.conn, "lab", "42", lane="strategy")
        claim = coordination.claim_task(self.conn, "strategy-worker", preferred_lane="strategy")
        self.assertTrue(coordination.renew_task_lease(
            self.conn, task["task_id"], "strategy-worker", claim["claim_token"], state="coding"
        ))
        self.assertTrue(coordination.requeue_task(
            self.conn, task["task_id"], worker_id="strategy-worker", claim_token=claim["claim_token"], error="network"
        ))
        claim = coordination.claim_task(self.conn, "strategy-worker", preferred_lane="strategy")
        self.assertTrue(coordination.complete_task(
            self.conn, task["task_id"], worker_id="strategy-worker", claim_token=claim["claim_token"],
            status="promoted_pending_verification", result={"commit": "abc"}
        ))
        row = self.conn.execute("select status,result_json from codex_tasks where task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual("promoted_pending_verification", row["status"])
        self.assertEqual("abc", json.loads(row["result_json"])["commit"])

    def test_named_resource_leases_are_atomic_and_reclaim_dead_owner(self) -> None:
        first = coordination.acquire_resource_lease(
            self.conn, "main_promotion", "worker-a", pid=999_999, lease_seconds=0
        )
        self.assertIsNotNone(first)
        self.assertIsNone(coordination.acquire_resource_lease(
            self.conn, "main_promotion", "worker-b", pid=os.getpid(), lease_seconds=30,
            pid_alive=lambda pid: True,
        ))
        second = coordination.acquire_resource_lease(
            self.conn, "main_promotion", "worker-b", pid=os.getpid(), lease_seconds=30,
            pid_alive=lambda pid: False,
        )
        self.assertEqual("worker-b", second["owner_worker_id"])
        self.assertTrue(coordination.renew_resource_lease(
            self.conn, "main_promotion", "worker-b", second["lease_token"]
        ))
        self.assertTrue(coordination.release_resource_lease(
            self.conn, "main_promotion", "worker-b", second["lease_token"]
        ))

    def test_verification_jobs_are_deduplicated_claimed_and_finished(self) -> None:
        task = coordination.enqueue_task(self.conn, "proposal", "verify", lane="general")
        job = coordination.enqueue_verification_job(
            self.conn, task["task_id"], verification_kind="full_regression", priority=3, payload={"commit": "abc"}
        )
        duplicate = coordination.enqueue_verification_job(
            self.conn, task["task_id"], verification_kind="full_regression", priority=8
        )
        self.assertEqual(job["job_id"], duplicate["job_id"])
        claim = coordination.claim_verification_job(self.conn, "verify-1", pid=os.getpid())
        self.assertEqual(job["job_id"], claim["job_id"])
        self.assertTrue(coordination.finish_verification_job(
            self.conn, job["job_id"], worker_id="verify-1", claim_token=claim["claim_token"],
            status="verified", result={"tests": "pass"}, task_status="verified"
        ))
        self.assertEqual("verified", self.conn.execute(
            "select status from codex_tasks where task_id=?", (task["task_id"],)
        ).fetchone()[0])

    def test_new_candidate_revision_reactivates_completed_verification_job(self) -> None:
        task = coordination.enqueue_task(self.conn, "proposal", "repair-revision", lane="general")
        job = coordination.enqueue_verification_job(
            self.conn, task["task_id"], payload={"proposal_id": "p", "candidate_commit": "old"}
        )
        claim = coordination.claim_verification_job(self.conn, "verify-1", pid=os.getpid())
        coordination.finish_verification_job(
            self.conn, job["job_id"], worker_id="verify-1", claim_token=claim["claim_token"],
            status="failed_needs_repair", task_status="repairing_post_promotion",
        )

        reactivated = coordination.enqueue_verification_job(
            self.conn, task["task_id"], payload={"proposal_id": "p", "candidate_commit": "repaired"}
        )

        self.assertEqual(job["job_id"], reactivated["job_id"])
        self.assertEqual("queued", reactivated["status"])
        self.assertIsNone(reactivated["completed_at"])
        claimed = coordination.claim_verification_job(self.conn, "verify-2", pid=os.getpid())
        self.assertEqual("repaired", claimed["payload"]["candidate_commit"])

    def test_migration_metadata_and_json_summary_cover_queue_workers_promotions_and_repairs(self) -> None:
        coordination.record_migration(self.conn, "initial_import", {"tasks": 2})
        promoted = coordination.enqueue_task(self.conn, "proposal", "promoted", lane="general")
        repair = coordination.enqueue_task(self.conn, "proposal", "repair", lane="general")
        coordination.complete_task(self.conn, promoted["task_id"], status="promoted")
        coordination.complete_task(self.conn, repair["task_id"], status="repairing_post_promotion")
        coordination.heartbeat_worker(self.conn, "general-1", preferred_lane="general", state="idle")
        summary = coordination.coordination_summary(self.conn)
        json.dumps(summary)
        self.assertEqual(1, summary["promotions"])
        self.assertEqual(1, summary["repairs"])
        self.assertEqual({"tasks": 2}, summary["migrations"]["initial_import"]["details"])
        self.assertEqual("general-1", summary["workers"][0]["worker_id"])
        self.assertIn("deduplicated_tasks", summary)
        self.assertIn("shared_work", summary)


if __name__ == "__main__":
    unittest.main()
