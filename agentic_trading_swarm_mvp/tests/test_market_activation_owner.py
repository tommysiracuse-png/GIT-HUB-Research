from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_activation_owner as owner
from code_evolution import preflight_proposal
from storage import init_db


class MarketActivationOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.report_json = root / "public_adapters.json"
        self.owner_json = root / "owner.json"
        self.owner_md = root / "owner.md"
        self.settings = {
            "market_activation_owner": {
                "enabled": True,
                "max_new_tasks_per_cycle": 100,
                "max_strategy_handoffs_per_cycle": 20,
                "runtime_verification_scans": 3,
                "retry_backoff_seconds": 0,
            },
            "strategy_implementation_owner": {"salvage_invalid_backlog": False},
            "agent_memory": {"enabled": False},
        }

    def tearDown(self) -> None:
        self.conn.close()

    def _patch_paths(self):
        return mock.patch.multiple(
            owner,
            ADAPTER_REPORT_JSON=self.report_json,
            REPORT_JSON=self.owner_json,
            REPORT_MD=self.owner_md,
        )

    def _write_adapter_report(
        self,
        *,
        price_observations: int = 0,
        candidates: int = 0,
        source_status: str = "reachable",
    ) -> None:
        self.report_json.write_text(
            json.dumps(
                {
                    "generated_at": "2026-08-05T12:00:00+00:00",
                    "adapters": [
                        {
                            "adapter_id": "test_exchange_spot",
                            "venue": "TEST_EXCHANGE",
                            "market_type": "spot",
                            "source_status": source_status,
                            "observation_count": 4,
                            "price_observation_count": price_observations,
                            "candidate_count": candidates,
                            "research_only_count": 4,
                            "market_surfaces": {"test_exchange_spot_market": 4},
                            "sample_instruments": ["TEST_EXCHANGE:ABC_USD"],
                            "available_fields": ["inst_id", "last", "bid", "ask", "observed_at"],
                            "docs_url": "https://example.test/public-market-data",
                            "adapter_spec_id": 123,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _task(self) -> sqlite3.Row:
        return self.conn.execute("select * from market_activation_tasks").fetchone()

    def _insert_admission(self, stage: str = "priceable") -> None:
        self.conn.execute(
            """
            insert into market_admission_states(
                admission_key,venue,inst_id,data_source,market_surface,strategy_lineage,
                current_stage,highest_stage,health_status,blocker_code,session_status,
                attempts,eligible_scans,stalled_eligible_scans,consecutive_failures,
                first_seen_at,last_seen_at,last_advanced_at,details_json
            ) values(
                'admission-test','TEST_EXCHANGE','TEST_EXCHANGE:ABC_USD','public',
                'test_exchange_spot_market','adapter_observation',?,?, 'healthy',null,'continuous',
                4,4,0,0,'2026-08-05T12:00:00+00:00','2026-08-05T12:05:00+00:00',
                '2026-08-05T12:05:00+00:00',?
            )
            """,
            (stage, stage, json.dumps({"adapter_id": "test_exchange_spot"})),
        )
        self.conn.commit()

    def test_zero_candidate_adapter_creates_durable_activation_task(self) -> None:
        self._write_adapter_report(price_observations=0, candidates=0)
        with self._patch_paths():
            report = owner.run_once(self.conn, self.settings, execute_turn=False, cycle_id="cycle-1")

        task = self._task()
        self.assertEqual("test_exchange_spot", task["adapter_id"])
        self.assertEqual("test_exchange_spot_market", task["market_surface"])
        self.assertEqual("queued", task["status"])
        self.assertEqual(1, report["sync"]["tasks_created"])
        acceptance = json.loads(task["acceptance_json"])
        self.assertIn("paper_trade", acceptance["required_chain"])
        self.assertIn("reliable_outcome", acceptance["required_chain"])

    def test_priceable_surface_hands_off_to_strategy_owner_without_static_signal(self) -> None:
        self._write_adapter_report(price_observations=4, candidates=0)
        with self._patch_paths():
            owner.run_once(self.conn, self.settings, execute_turn=False, cycle_id="cycle-1")
        self._insert_admission("priceable")

        with self._patch_paths():
            report = owner.run_once(self.conn, self.settings, execute_turn=False, cycle_id="cycle-2")

        task = self._task()
        self.assertEqual("strategy_handoff", task["status"])
        self.assertTrue(task["strategy_owner_task_id"])
        self.assertEqual(1, len(report["strategy_handoffs"]))
        strategy_task = self.conn.execute(
            "select dependency_json from strategy_owner_tasks where task_id=?",
            (task["strategy_owner_task_id"],),
        ).fetchone()
        payload = json.loads(strategy_task["dependency_json"])["source_payload"]
        experiment = payload["strategy_lab_experiment"]
        self.assertEqual("observation_program", experiment["experiment_type"])
        self.assertNotIn("strategy_logic", experiment)
        self.assertEqual("test_exchange_spot", experiment["data_requirements"]["adapter_id"])

    def test_codex_turn_owns_full_runtime_chain_not_just_adapter_file(self) -> None:
        self._write_adapter_report(price_observations=0, candidates=0)
        captured = {}

        def fake_process(conn, recommendation, settings):
            captured.update(recommendation["payload"])
            return [{"proposal_id": "activation-proposal", "status": "promoted"}]

        with self._patch_paths(), mock.patch.object(owner, "process_code_change_recommendation", side_effect=fake_process):
            report = owner.run_once(self.conn, self.settings, execute_turn=True, cycle_id="cycle-code")

        task = self._task()
        self.assertEqual("deployed_waiting_runtime", task["status"])
        self.assertTrue(report["consumed_writer"])
        self.assertEqual("runtime_pipeline_integration", captured["change_category"])
        contract = captured["code_change"]["activation_contract"]
        self.assertIn("strategy_lab_handoff_or_surface_candidate", contract["required_chain"])
        self.assertIn("paper_trade", contract["required_chain"])
        self.assertIn("reliable_outcome", contract["required_chain"])
        self.assertIn("never fabricate a price", captured["proposed_change"])
        preflight = preflight_proposal(captured, self.settings)
        self.assertFalse(preflight["quality_scorecard"]["reject_before_model_call"])

    def test_paused_code_turn_records_paper_only_diagnostic_pass(self) -> None:
        self._write_adapter_report(price_observations=4, candidates=0)
        captured = {}

        def fake_process(conn, recommendation, settings):
            captured.update(recommendation["payload"])
            return [{"proposal_id": "activation-proposal", "status": "implementation_paused"}]

        with self._patch_paths(), mock.patch.object(owner, "process_code_change_recommendation", side_effect=fake_process):
            owner.run_once(self.conn, self.settings, execute_turn=True, cycle_id="cycle-paused")

        task = self._task()
        self.assertEqual("implementation_paused", task["status"])
        last_result = json.loads(task["last_result_json"])
        diagnostic = captured["evidence"]["paper_diagnostic_pass"]
        self.assertTrue(diagnostic["paper_only"])
        self.assertIn("data_freshness", diagnostic)
        self.assertIn("signal_values", diagnostic)
        self.assertIn("decision_thresholds", diagnostic)
        self.assertIn("simulated_positions", diagnostic)
        self.assertIn("json_schema_validation", diagnostic)
        self.assertEqual(4, diagnostic["signal_values"]["price_observation_count"])
        self.assertEqual(0, diagnostic["signal_values"]["candidate_count"])
        self.assertEqual(
            diagnostic,
            last_result["paper_diagnostic_pass"],
        )
        self.assertEqual(
            diagnostic,
            last_result["active_code_recommendation"]["payload"]["evidence"]["paper_diagnostic_pass"],
        )

    def test_task_completes_only_after_matching_paper_trade_has_reliable_outcome(self) -> None:
        self._write_adapter_report(price_observations=4, candidates=1)
        with self._patch_paths():
            owner.run_once(self.conn, self.settings, execute_turn=False, cycle_id="cycle-1")
        task = self._task()
        candidate = {
            "adapter_id": "test_exchange_spot",
            "market_surface": "test_exchange_spot_market",
            "venue": "TEST_EXCHANGE",
            "inst_id": "TEST_EXCHANGE:ABC_USD",
            "direction": "long",
        }
        opened_at = owner._utc_now()
        self.conn.execute(
            """
            insert into paper_trades(
                opened_at,venue,inst_id,direction,trade_type,signal_key,base_score,learned_score,
                entry,status,thesis,candidate_json,review_json
            ) values(
                ?,'TEST_EXCHANGE','TEST_EXCHANGE:ABC_USD','long',
                'market_activation_test','ACTIVATION|TEST',1,1,100,'closed','test',?, '{}'
            )
            """,
            (opened_at, json.dumps(candidate)),
        )
        trade_id = int(self.conn.execute("select last_insert_rowid()").fetchone()[0])
        self.conn.execute(
            """
            insert into paper_trade_outcomes(
                trade_id,horizon_minutes,measured_at,price,pnl_bps,context_json,target_at,
                observed_at,delay_seconds,measurement_status,price_source
            ) values(?,60,'2026-08-05T13:10:00+00:00',101,100,'{}',
                     '2026-08-05T13:10:00+00:00','2026-08-05T13:10:10+00:00',10,'valid','public')
            """,
            (trade_id,),
        )
        self.conn.commit()

        with self._patch_paths():
            report = owner.run_once(self.conn, self.settings, execute_turn=False, cycle_id="cycle-2")

        task = self._task()
        self.assertEqual("completed_paper_evaluated", task["status"])
        self.assertEqual(1, report["summary"]["funnel"]["paper_evaluated"])


if __name__ == "__main__":
    unittest.main()
