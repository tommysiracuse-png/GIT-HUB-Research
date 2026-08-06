from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
