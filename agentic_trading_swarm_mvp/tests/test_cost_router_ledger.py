from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cost_router  # noqa: E402
import storage  # noqa: E402


class CostRouterLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.db_path = root / "cost.sqlite"
        self.deferred_path = root / "deferred.jsonl"
        with storage.connect(self.db_path):
            pass
        self.connect_patch = mock.patch.object(
            cost_router,
            "connect",
            side_effect=lambda initialize=True: storage.connect(
                self.db_path,
                initialize=initialize,
            ),
        )
        self.deferred_patch = mock.patch.object(
            cost_router,
            "COST_LOG_DEFERRED_PATH",
            self.deferred_path,
        )
        self.connect_patch.start()
        self.deferred_patch.start()

    def tearDown(self) -> None:
        self.deferred_patch.stop()
        self.connect_patch.stop()
        self.temp.cleanup()

    def config(self, *, call_limit: int = 10, budget: float = 25.0) -> dict:
        return {
            "daily_budget_usd": budget,
            "rolling_24h_budget_usd": budget,
            "daily_call_limit": call_limit,
            "rolling_24h_call_limit": call_limit,
        }

    def reserve(
        self,
        *,
        cfg: dict | None = None,
        output_tokens: int = 100,
        agent_cfg: dict | None = None,
    ) -> dict:
        tier = {
            "input_cost_per_1m": 1.0,
            "output_cost_per_1m": 1.0,
            "estimated_completion_tokens": output_tokens,
        }
        return cost_router._reserve_model_call(
            agent_name="test-agent",
            cfg=cfg or self.config(),
            agent_cfg=agent_cfg or {"daily_budget_usd": 25.0},
            tier_name="fast",
            tier_cfg=tier,
            model_name="local/test",
            prompt_tokens=100,
            max_output_tokens=output_tokens,
            provider="local",
            api="litellm",
            reasoning_effort=None,
            verbosity=None,
            operation="test",
            prompt_cache_key=None,
            frontier_escalation_reason=None,
            structured_json=False,
        )

    def insert_event(self, *, created_at: str, status: str, cost: float = 0.0) -> None:
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute(
                """
                insert into llm_cost_events (
                    created_at,agent_name,model_tier,model_name,prompt_tokens,
                    completion_tokens,estimated_cost_usd,status
                ) values (?,?,?,?,?,?,?,?)
                """,
                (created_at, "legacy", "fast", "legacy-model", 1, 1, cost, status),
            )
            conn.commit()

    def deferred_payload(self, **overrides: object) -> dict:
        payload = {
            "agent_name": "deferred-agent",
            "model_tier": "fast",
            "model_name": "local/test",
            "provider": "local",
            "api": "litellm",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "estimated_cost_usd": 0.01,
            "status": "model_call:litellm",
            "created_at": "2026-08-07T01:00:00+00:00",
        }
        payload.update(overrides)
        return payload

    def test_exact_legacy_model_call_counts_toward_ten_call_limit(self) -> None:
        now = dt.datetime(2026, 8, 7, 12, tzinfo=dt.timezone.utc)
        for index in range(10):
            self.insert_event(
                created_at=(now - dt.timedelta(minutes=index)).isoformat(),
                status="model_call",
            )
        with mock.patch.object(cost_router, "_utc_now", return_value=now):
            result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertIn("global_utc_call_guard", result["status"])

    def test_atomic_reservations_stop_the_eleventh_attempt(self) -> None:
        results = [self.reserve() for _ in range(11)]
        self.assertTrue(all(row["allowed"] for row in results[:10]))
        self.assertFalse(results[10]["allowed"])
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute(
                "select count(*) from llm_cost_events where status='model_call_reserved'"
            ).fetchone()[0]
        self.assertEqual(10, count)

    def test_zero_call_limit_disables_paid_reservations(self) -> None:
        result = self.reserve(cfg=self.config(call_limit=0))
        self.assertFalse(result["allowed"])
        self.assertIn("global_utc_call_guard", result["status"])

    def test_zero_cost_budget_disables_paid_reservations(self) -> None:
        result = self.reserve(
            cfg=self.config(call_limit=10, budget=0.0),
            agent_cfg={"daily_budget_usd": 0.0},
        )
        self.assertFalse(result["allowed"])
        self.assertIn("global_utc_budget_guard", result["status"])

    def test_negative_limits_fail_closed_in_locked_profile(self) -> None:
        result = self.reserve(cfg=self.config(call_limit=-1, budget=-1.0))
        self.assertFalse(result["allowed"])
        self.assertEqual("cost_limit_config_invalid", result["status"])

    def test_global_limits_above_locked_ceiling_fail_closed(self) -> None:
        over_budget = self.reserve(cfg=self.config(call_limit=10, budget=25.01))
        over_calls = self.reserve(cfg=self.config(call_limit=11, budget=25.0))
        self.assertFalse(over_budget["allowed"])
        self.assertFalse(over_calls["allowed"])
        self.assertEqual("cost_limit_config_invalid", over_budget["status"])
        self.assertEqual("cost_limit_config_invalid", over_calls["status"])
        status = cost_router.cost_budget_status(
            self.config(call_limit=11, budget=25.01),
            agent_name="test-agent",
            replay_deferred=False,
        )
        self.assertFalse(status["allowed"])
        self.assertEqual("cost_budget_unavailable", status["status"])
        self.assertIn("cost_limit_config_invalid", status["reason"])

    def test_agent_limits_cannot_exceed_global_limits(self) -> None:
        result = self.reserve(
            cfg=self.config(call_limit=5, budget=5.0),
            agent_cfg={
                "daily_budget_usd": 5.01,
                "daily_call_limit": 6,
            },
        )
        self.assertFalse(result["allowed"])
        self.assertEqual("cost_limit_config_invalid", result["status"])

    def test_budget_status_fails_when_next_call_would_exceed_call_limit(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.insert_event(created_at=now.isoformat(), status="model_call", cost=0.01)
        cfg = self.config(call_limit=1)
        status = cost_router.cost_budget_status(
            cfg,
            agent_name="test-agent",
            replay_deferred=False,
        )
        self.assertFalse(status["allowed"])
        self.assertEqual("cost_budget_exhausted", status["status"])
        self.assertIn("global_utc_call_guard", status["reason"])

    def test_budget_status_treats_zero_agent_budget_as_disabled(self) -> None:
        cfg = {
            **self.config(call_limit=10),
            "agents": {"test-agent": {"daily_budget_usd": 0.0}},
        }
        status = cost_router.cost_budget_status(
            cfg,
            agent_name="test-agent",
            replay_deferred=False,
        )
        self.assertFalse(status["allowed"])
        self.assertEqual("cost_budget_exhausted", status["status"])
        self.assertIn("agent_utc_budget_guard", status["reason"])

    def test_budget_status_fails_at_exact_cost_ceiling(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.insert_event(created_at=now.isoformat(), status="model_call", cost=25.0)
        status = cost_router.cost_budget_status(
            self.config(call_limit=10, budget=25.0),
            agent_name="test-agent",
            replay_deferred=False,
        )
        self.assertFalse(status["allowed"])
        self.assertIn("global_utc_budget_guard", status["reason"])

    def test_budget_status_never_implicitly_replays_pending_ledger(self) -> None:
        self.deferred_path.write_text(
            json.dumps(self.deferred_payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        source_bytes = self.deferred_path.read_bytes()

        default_status = cost_router.cost_budget_status(self.config())
        legacy_true_status = cost_router.cost_budget_status(
            self.config(),
            replay_deferred=True,
        )

        self.assertFalse(default_status["allowed"])
        self.assertEqual("cost_budget_unavailable", default_status["status"])
        self.assertEqual("deferred_cost_log_pending", default_status["reason"])
        self.assertFalse(default_status["deferred_replay"]["mutation_performed"])
        self.assertFalse(legacy_true_status["allowed"])
        self.assertEqual("cost_budget_unavailable", legacy_true_status["status"])
        self.assertIn("explicit_maintenance_required", legacy_true_status["reason"])
        self.assertTrue(
            legacy_true_status["deferred_replay"]["legacy_replay_requested"]
        )
        self.assertFalse(
            legacy_true_status["deferred_replay"]["mutation_performed"]
        )
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute("select count(*) from llm_cost_events").fetchone()[0]
        self.assertEqual(0, count)
        self.assertEqual(source_bytes, self.deferred_path.read_bytes())

    def test_invalid_estimated_cost_fails_closed(self) -> None:
        usage = {
            "global": {
                "utc_day": {"cost_usd": 0.0, "calls": 0},
                "rolling_24h": {"cost_usd": 0.0, "calls": 0},
            },
            "agent": {
                "utc_day": {"cost_usd": 0.0, "calls": 0},
                "rolling_24h": {"cost_usd": 0.0, "calls": 0},
            },
        }
        reason = cost_router._budget_reason(
            usage,
            cfg=self.config(),
            agent_cfg={"daily_budget_usd": 25.0},
            estimated_call_cost=float("nan"),
        )
        self.assertEqual("estimated_call_cost_invalid", reason)

    def test_concurrent_reservations_cannot_oversubscribe_call_cap(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _index: self.reserve(), range(20)))
        self.assertEqual(10, sum(bool(row["allowed"]) for row in results))
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute(
                "select count(*) from llm_cost_events where status='model_call_reserved'"
            ).fetchone()[0]
        self.assertEqual(10, count)

    def test_rolling_window_blocks_previous_utc_day_attempt(self) -> None:
        now = dt.datetime(2026, 8, 7, 0, 30, tzinfo=dt.timezone.utc)
        self.insert_event(
            created_at=(now - dt.timedelta(minutes=45)).isoformat(),
            status="model_call",
        )
        with mock.patch.object(cost_router, "_utc_now", return_value=now):
            result = self.reserve(cfg=self.config(call_limit=1))
        self.assertFalse(result["allowed"])
        self.assertIn("global_rolling_24h_call_guard", result["status"])

    def test_utc_day_normalizes_timestamp_offsets(self) -> None:
        now = dt.datetime(2026, 8, 7, 0, 30, tzinfo=dt.timezone.utc)
        # The textual date is August 6, but the instant is August 7 UTC.
        self.insert_event(
            created_at="2026-08-06T20:15:00-04:00",
            status="model_call",
        )
        with storage.connect(self.db_path, initialize=False) as conn:
            usage = cost_router._window_usage(conn, now=now)
        self.assertEqual(1, usage["utc_day"]["calls"])

    def test_cost_limit_reserves_worst_case_output_before_provider_call(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.insert_event(created_at=now.isoformat(), status="model_call", cost=24.99)
        result = self.reserve(cfg=self.config(call_limit=10), output_tokens=20_000)
        self.assertFalse(result["allowed"])
        self.assertIn("budget_guard", result["status"])

    def test_deferred_legacy_replay_is_timestamped_and_idempotent(self) -> None:
        payloads = [
            {
                "agent_name": "a",
                "model_tier": "fast",
                "model_name": "m",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "estimated_cost_usd": 0.01,
                "status": "model_call",
            },
            {
                "agent_name": "b",
                "model_tier": "fast",
                "model_name": "m",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "estimated_cost_usd": 0.02,
                "status": "model_call:responses",
            },
        ]
        self.deferred_path.write_text(
            "\n".join(json.dumps(row) for row in payloads) + "\n",
            encoding="utf-8",
        )
        stamp = dt.datetime(2026, 8, 7, 1, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.deferred_path, (stamp, stamp))
        source_bytes = self.deferred_path.read_bytes()
        first = cost_router.replay_deferred_cost_events()
        with storage.connect(self.db_path, initialize=False) as conn:
            first_max_id = conn.execute("select max(id) from llm_cost_events").fetchone()[0]
        second = cost_router.replay_deferred_cost_events()
        self.assertEqual(2, first["inserted"])
        self.assertEqual(2, first["inferred_timestamps"])
        self.assertTrue(first["complete"])
        self.assertTrue(first["post_verification"]["complete"])
        self.assertEqual(2, second["skipped"])
        self.assertTrue(second["complete"])
        with storage.connect(self.db_path, initialize=False) as conn:
            rows = conn.execute(
                "select event_id,created_at from llm_cost_events order by created_at"
            ).fetchall()
            second_max_id = conn.execute("select max(id) from llm_cost_events").fetchone()[0]
        self.assertEqual(2, len(rows))
        self.assertTrue(all(str(row["event_id"]).startswith("deferred-") for row in rows))
        self.assertEqual(first_max_id, second_max_id)
        self.assertEqual(source_bytes, self.deferred_path.read_bytes())
        self.assertEqual(2, len(self.deferred_path.read_text(encoding="utf-8").splitlines()))

    def test_deferred_reconciliation_is_read_only_and_reports_exact_pending_rows(self) -> None:
        payload = self.deferred_payload()
        raw = json.dumps(payload, sort_keys=True) + "\n"
        self.deferred_path.write_text(raw, encoding="utf-8")
        source_bytes = self.deferred_path.read_bytes()

        with storage.connect(self.db_path, initialize=False) as conn:
            before = conn.execute("select count(*) from llm_cost_events").fetchone()[0]
            status = cost_router.deferred_cost_reconciliation_status(
                conn,
                include_event_ids=True,
            )
            after = conn.execute("select count(*) from llm_cost_events").fetchone()[0]

        self.assertFalse(status["complete"])
        self.assertEqual(1, status["read"])
        self.assertEqual(1, status["pending"])
        self.assertEqual(0, status["invalid"])
        self.assertEqual(0, status["reserved"])
        self.assertEqual(0, status["conflicting"])
        self.assertEqual(0, status["reconciled"])
        self.assertEqual(1, status["unique_event_count"])
        self.assertEqual(1, len(status["event_ids"]))
        self.assertEqual(status["event_ids"], status["pending_event_ids"])
        self.assertEqual(0.01, status["expected_cost_usd"])
        self.assertEqual(before, after)
        self.assertEqual(source_bytes, self.deferred_path.read_bytes())

    def test_missing_deferred_ledger_has_stable_empty_source_digest(self) -> None:
        status = cost_router.deferred_cost_reconciliation_status()

        self.assertTrue(status["complete"])
        self.assertFalse(status["source_exists"])
        self.assertEqual(0, status["read"])
        self.assertEqual(
            "e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855",
            status["source_digest"],
        )

    def test_valid_unreplayed_ledger_blocks_reservation_without_implicit_replay(self) -> None:
        self.deferred_path.write_text(
            json.dumps(self.deferred_payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        source_bytes = self.deferred_path.read_bytes()

        result = self.reserve()

        self.assertFalse(result["allowed"])
        self.assertEqual("deferred_cost_log_pending", result["status"])
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute("select count(*) from llm_cost_events").fetchone()[0]
        self.assertEqual(0, count)
        self.assertEqual(source_bytes, self.deferred_path.read_bytes())

    def test_identical_legacy_lines_receive_distinct_line_derived_ids(self) -> None:
        payload = self.deferred_payload(created_at=None)
        payload.pop("created_at")
        line = json.dumps(payload, sort_keys=True)
        self.deferred_path.write_text(f"{line}\n{line}\n", encoding="utf-8")

        replay = cost_router.replay_deferred_cost_events()

        self.assertTrue(replay["complete"])
        self.assertEqual(2, replay["inserted"])
        self.assertEqual(2, len(replay["event_ids"]))
        self.assertEqual(2, len(set(replay["event_ids"])))
        self.assertTrue(all(value.startswith("deferred-") for value in replay["event_ids"]))
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute("select count(*) from llm_cost_events").fetchone()[0]
        self.assertEqual(2, count)

    def test_append_does_not_change_replayed_legacy_timestamp_identity(self) -> None:
        legacy = self.deferred_payload()
        legacy.pop("created_at")
        legacy_line = json.dumps(legacy, sort_keys=True)
        self.deferred_path.write_text(legacy_line + "\n", encoding="utf-8")
        first_stamp = dt.datetime(2026, 8, 7, 1, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.deferred_path, (first_stamp, first_stamp))
        first = cost_router.replay_deferred_cost_events()
        self.assertTrue(first["complete"])
        with storage.connect(self.db_path, initialize=False) as conn:
            original_created_at = conn.execute(
                "select created_at from llm_cost_events where event_id=?",
                (first["event_ids"][0],),
            ).fetchone()[0]

        appended = self.deferred_payload(event_id="new-explicit-event")
        self.deferred_path.write_text(
            legacy_line + "\n" + json.dumps(appended, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        later_stamp = dt.datetime(2026, 8, 8, 1, tzinfo=dt.timezone.utc).timestamp()
        os.utime(self.deferred_path, (later_stamp, later_stamp))

        pending = cost_router.deferred_cost_reconciliation_status(include_event_ids=True)
        self.assertFalse(pending["complete"])
        self.assertEqual(1, pending["reconciled"])
        self.assertEqual(1, pending["pending"])
        self.assertEqual(0, pending["conflicting"])
        replay = cost_router.replay_deferred_cost_events()
        self.assertTrue(replay["complete"])
        self.assertEqual(1, replay["inserted"])
        with storage.connect(self.db_path, initialize=False) as conn:
            durable_created_at = conn.execute(
                "select created_at from llm_cost_events where event_id=?",
                (first["event_ids"][0],),
            ).fetchone()[0]
        self.assertEqual(original_created_at, durable_created_at)

    def test_conflicting_existing_event_id_fails_closed_without_overwrite(self) -> None:
        payload = self.deferred_payload(event_id="fixed-event")
        self.deferred_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        first = cost_router.replay_deferred_cost_events()
        self.assertTrue(first["complete"])
        with storage.connect(self.db_path, initialize=False) as conn:
            conn.execute(
                "update llm_cost_events set estimated_cost_usd=? where event_id=?",
                (9.99, "fixed-event"),
            )
            conn.commit()

        source_before = self.deferred_path.read_bytes()
        status = cost_router.deferred_cost_reconciliation_status(include_event_ids=True)
        replay = cost_router.replay_deferred_cost_events()

        self.assertFalse(status["complete"])
        self.assertEqual(1, status["conflicting"])
        self.assertEqual(["fixed-event"], status["conflicting_event_ids"])
        self.assertFalse(replay["complete"])
        self.assertEqual("deferred_cost_log_conflict", replay["status"])
        self.assertEqual(0, replay["inserted"])
        self.assertEqual(0, replay["finalized"])
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select estimated_cost_usd,status from llm_cost_events where event_id=?",
                ("fixed-event",),
            ).fetchone()
        self.assertAlmostEqual(9.99, float(row["estimated_cost_usd"]))
        self.assertEqual("model_call:litellm", row["status"])
        self.assertEqual(source_before, self.deferred_path.read_bytes())

    def test_divergent_duplicate_explicit_id_blocks_entire_replay(self) -> None:
        first = self.deferred_payload(event_id="duplicate-event")
        second = self.deferred_payload(event_id="duplicate-event", estimated_cost_usd=0.02)
        self.deferred_path.write_text(
            json.dumps(first) + "\n" + json.dumps(second) + "\n",
            encoding="utf-8",
        )

        replay = cost_router.replay_deferred_cost_events()

        self.assertFalse(replay["complete"])
        self.assertEqual(2, replay["conflicting"])
        self.assertEqual(0, replay["inserted"])
        with storage.connect(self.db_path, initialize=False) as conn:
            count = conn.execute("select count(*) from llm_cost_events").fetchone()[0]
        self.assertEqual(0, count)

    def test_deferred_completion_finalizes_existing_reservation(self) -> None:
        reserved = self.reserve()
        result = cost_router.ModelResult(
            text="ok",
            model_name="local/test",
            model_tier="fast",
            prompt_tokens=100,
            completion_tokens=7,
            estimated_cost_usd=0.003,
            status="model_call:litellm",
            provider="local",
            api="litellm",
            operation="test",
            event_id=reserved["event_id"],
            created_at=reserved["created_at"],
        )
        cost_router._defer_cost_log("test-agent", result, reason="test")
        pending = cost_router.deferred_cost_reconciliation_status(include_event_ids=True)
        self.assertFalse(pending["complete"])
        self.assertEqual(1, pending["reserved"])
        self.assertEqual([reserved["event_id"]], pending["reserved_event_ids"])
        replay = cost_router.replay_deferred_cost_events()
        self.assertEqual(1, replay["finalized"])
        self.assertTrue(replay["complete"])
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select status,estimated_cost_usd from llm_cost_events where event_id=?",
                (reserved["event_id"],),
            ).fetchone()
        self.assertEqual("model_call:litellm", row["status"])
        self.assertAlmostEqual(0.003, row["estimated_cost_usd"])

    def test_deferred_reservation_metadata_mismatch_is_conflict(self) -> None:
        reserved = self.reserve()
        result = cost_router.ModelResult(
            text="ok",
            model_name="local/different-model",
            model_tier="fast",
            prompt_tokens=12,
            completion_tokens=7,
            estimated_cost_usd=0.003,
            status="model_call:litellm",
            provider="local",
            api="litellm",
            operation="test",
            event_id=reserved["event_id"],
            created_at=reserved["created_at"],
        )
        cost_router._defer_cost_log("test-agent", result, reason="test")

        reconciliation = cost_router.deferred_cost_reconciliation_status()
        replay = cost_router.replay_deferred_cost_events()

        self.assertFalse(reconciliation["complete"])
        self.assertEqual(1, reconciliation["conflicting"])
        self.assertFalse(replay["complete"])
        self.assertEqual(0, replay["finalized"])
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select status,model_name from llm_cost_events where event_id=?",
                (reserved["event_id"],),
            ).fetchone()
        self.assertEqual("model_call_reserved", row["status"])
        self.assertEqual("local/test", row["model_name"])

    def test_replayed_success_and_charged_failure_count_in_both_cost_windows(self) -> None:
        now = dt.datetime(2026, 8, 7, 12, tzinfo=dt.timezone.utc)
        success = self.deferred_payload(
            event_id="success-event",
            created_at=(now - dt.timedelta(hours=1)).isoformat(),
            status="model_call:responses",
            estimated_cost_usd=0.4,
        )
        failure = self.deferred_payload(
            event_id="failure-event",
            created_at=(now - dt.timedelta(hours=2)).isoformat(),
            status="fallback_error:provider",
            estimated_cost_usd=0.6,
        )
        self.deferred_path.write_text(
            json.dumps(success) + "\n" + json.dumps(failure) + "\n",
            encoding="utf-8",
        )

        replay = cost_router.replay_deferred_cost_events()
        with storage.connect(self.db_path, initialize=False) as conn:
            usage = cost_router._window_usage(conn, now=now)

        self.assertTrue(replay["complete"])
        self.assertEqual(2, usage["utc_day"]["calls"])
        self.assertEqual(2, usage["rolling_24h"]["calls"])
        self.assertAlmostEqual(1.0, usage["utc_day"]["cost_usd"])
        self.assertAlmostEqual(1.0, usage["rolling_24h"]["cost_usd"])

    def test_invalid_deferred_ledger_blocks_new_reservation(self) -> None:
        self.deferred_path.write_text("not-json\n", encoding="utf-8")
        result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertIn("invalid", result["status"])

    def test_unknown_deferred_cost_blocks_new_reservation(self) -> None:
        self.deferred_path.write_text(
            json.dumps({"agent_name": "a", "status": "model_call"}) + "\n",
            encoding="utf-8",
        )
        result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertIn("invalid", result["status"])

    def test_provider_error_retains_reserved_upper_bound_cost(self) -> None:
        cfg = {
            **self.config(call_limit=10),
            "require_env_to_call_models": True,
            "tiers": {
                "fast": {
                    "model": "local/test",
                    "api": "litellm",
                    "input_cost_per_1m": 1.0,
                    "output_cost_per_1m": 2.0,
                    "max_prompt_chars": 100,
                    "max_output_tokens": 100,
                }
            },
            "agents": {"test-agent": {"tier": "fast", "daily_budget_usd": 25.0}},
        }
        with mock.patch.dict(
            os.environ,
            {
                "RADAR_USE_LITELLM": "1",
                "RADAR_MODEL_CREDENTIAL_LOCK": "0",
                "RADAR_MODELS_DISABLED": "0",
            },
            clear=False,
        ), mock.patch.object(cost_router, "load_llm_config", return_value=cfg), mock.patch.object(
            cost_router,
            "claim_autonomous_paid_attempt",
            return_value={"allowed": True},
        ), mock.patch.object(
            cost_router,
            "_complete_litellm",
            side_effect=RuntimeError("provider outcome unknown"),
        ):
            result = cost_router.complete("test-agent", "prompt")
        self.assertTrue(result.status.startswith("fallback_error:"))
        self.assertGreater(result.estimated_cost_usd, 0.0)
        self.assertEqual("provider_error_cost_reserved_upper_bound", result.stop_reason)
        with storage.connect(self.db_path, initialize=False) as conn:
            row = conn.execute(
                "select status,estimated_cost_usd from llm_cost_events where event_id=?",
                (result.event_id,),
            ).fetchone()
        self.assertTrue(str(row["status"]).startswith("fallback_error:"))
        self.assertAlmostEqual(result.estimated_cost_usd, float(row["estimated_cost_usd"]))

    def test_credential_lock_wins_even_when_key_and_provider_flag_are_present(self) -> None:
        cfg = {
            "require_env_to_call_models": True,
            "tiers": {
                "fast": {
                    "model": "openai/test",
                    "api": "responses",
                    "estimated_completion_tokens": 10,
                    "max_output_tokens": 10,
                }
            },
            "agents": {"test-agent": {"tier": "fast"}},
        }
        environment = {
            "RADAR_USE_LITELLM": "1",
            "RADAR_MODEL_CREDENTIAL_LOCK": "1",
            "RADAR_MODELS_DISABLED": "1",
            "RADAR_PROCESS_ROLE": "bounded_paper_radar",
            "RADAR_RESEARCH_MODEL_OVERRIDE": "",
            "OPENAI_API_KEY": "present",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            cost_router, "load_llm_config", return_value=cfg
        ), mock.patch.object(cost_router, "_log"), mock.patch.object(
            cost_router, "_complete_openai_responses"
        ) as provider:
            result = cost_router.complete("test-agent", "test")
        self.assertEqual("credential_model_lock", result.status)
        provider.assert_not_called()

    def test_research_override_requires_both_explicit_scope_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RADAR_MODEL_CREDENTIAL_LOCK": "1",
                "RADAR_MODELS_DISABLED": "1",
                "RADAR_PROCESS_ROLE": "research_one_shot",
                "RADAR_RESEARCH_MODEL_OVERRIDE": "1",
            },
            clear=False,
        ):
            self.assertFalse(cost_router._model_credentials_locked())
            os.environ["RADAR_PROCESS_ROLE"] = "bounded_paper_radar"
            self.assertTrue(cost_router._model_credentials_locked())


if __name__ == "__main__":
    unittest.main()
