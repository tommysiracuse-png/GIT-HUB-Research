from __future__ import annotations

import copy
import datetime as dt
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_expansion_campaign as campaign  # noqa: E402
from settings import load_settings  # noqa: E402
from storage import connect  # noqa: E402


class BoundedRolloutSimulationTests(unittest.TestCase):
    def _operational_metrics(self) -> dict:
        return {
            "cycle_success": True,
            "exit_code": 0,
            "runtime_seconds": 100.0,
            "peak_rss_mb": 500.0,
            "supervisor_count": 1,
            "child_count": 1,
            "forbidden_worker_count": 0,
            "terminal_opportunity_rate": 1.0,
            "frontier_observation_count": 6000,
            "reachable_venue_count": 16,
            "db_growth_bytes": 0,
            "db_footprint_start_bytes": 0,
            "db_footprint_bytes": 0,
            "artifact_sizes": {},
        }

    def _measurement_metrics(self, cycle_index: int) -> dict:
        metrics = self._operational_metrics()
        close = int(cycle_index <= 250)
        phase_close_count = min(cycle_index, 250)
        metrics.update(
            {
                "phase_distinct_exact_attributed_admission_keys_paper_evaluated": min(
                    cycle_index, 100
                ),
                "new_direct_closes": close,
                "new_reliable_direct_closes": close,
                "new_timely_direct_closes": close,
                "phase_due_direct_closes": phase_close_count,
                "phase_reliable_direct_closes": phase_close_count,
                "phase_timely_direct_closes": phase_close_count,
                "new_horizon_outcomes": close,
                "new_timely_horizon_outcomes": close,
                "phase_due_horizon_outcomes": phase_close_count,
                "phase_timely_horizon_outcomes": phase_close_count,
                "new_opportunity_lineage_records": close,
                "new_opportunity_lineage_complete": close,
                "new_order_lineage_records": close,
                "new_order_lineage_complete": close,
                "new_trade_lineage_records": close,
                "new_trade_lineage_complete": close,
                "new_synthetic_proxy_primary": 0,
                "lineage_corruption_count": 0,
            }
        )
        return metrics

    def test_accelerated_full_rollout_reaches_research_without_paid_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "RADAR_PROCESS_ROLE": "bounded_paper_radar",
                "RADAR_BOUNDED_SUPERVISOR_COUNT": "1",
                "RADAR_BOUNDED_CHILD_COUNT": "1",
                "RADAR_FORBIDDEN_WORKER_COUNT": "0",
            },
        ):
            root = pathlib.Path(tmp)
            settings = copy.deepcopy(
                load_settings(ROOT / "config" / "settings.bounded_crypto_paper.json")
            )
            settings["paper_expansion"]["deferred_cost_path"] = str(
                root / "deferred.jsonl"
            )
            settings["paper_expansion"]["autonomous_ledger_path"] = str(
                root / "attempts.sqlite"
            )
            base = dt.datetime(2026, 8, 7, tzinfo=dt.timezone.utc)
            with connect(root / "radar.sqlite") as conn:

                def run_cycle(at: dt.datetime, metrics: dict) -> dict:
                    with mock.patch.object(campaign, "_utc_now", return_value=at):
                        _effective, token = campaign.apply_campaign_controls(conn, settings)
                        return campaign.record_campaign_cycle(conn, token, metrics)

                report = run_cycle(base, self._operational_metrics())
                for index in range(1, 97):
                    report = run_cycle(
                        base + dt.timedelta(minutes=15 * index),
                        self._operational_metrics(),
                    )
                self.assertEqual("measurement", report["phase_after"])

                measurement_start = base + dt.timedelta(hours=24)
                for index in range(1, 673):
                    report = run_cycle(
                        measurement_start + dt.timedelta(minutes=15 * index),
                        self._measurement_metrics(index),
                    )
                self.assertEqual("canary", report["phase_after"])

                canary_start = measurement_start + dt.timedelta(hours=168)
                for index in range(1, 193):
                    metrics = self._operational_metrics()
                    metrics.update(
                        {
                            "active_canary_count": 1,
                            "new_canary_reliable_direct_labels": int(index <= 30),
                            "phase_canary_reliable_direct_labels": min(index, 30),
                        }
                    )
                    report = run_cycle(
                        canary_start + dt.timedelta(minutes=15 * index),
                        metrics,
                    )
                self.assertEqual("research", report["phase_after"])
                self.assertTrue(report["research_ready"])
                self.assertEqual(
                    0,
                    conn.execute("select count(*) from llm_cost_events").fetchone()[0],
                )
                self.assertEqual(
                    0,
                    conn.execute(
                        "select count(*) from execution_orders where lower(mode)='live'"
                    ).fetchone()[0],
                )
                self.assertGreaterEqual(report["state"]["total_cycle_count"], 961)


if __name__ == "__main__":
    unittest.main()
