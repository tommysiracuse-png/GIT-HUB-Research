from __future__ import annotations

import copy
import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import learning  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db, open_paper_trade  # noqa: E402


class BoundedLearningLineageTests(unittest.TestCase):
    def test_only_queue_linked_trade_can_update_primary_learning(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        try:
            legacy = copy.deepcopy(DEFAULT_SETTINGS)
            legacy["market_admission"]["paper_queue_enabled"] = False
            review = {
                "decision": "approve_paper_trade",
                "learned_score": 70.0,
                "hard_blocks": [],
                "route_status": "standard",
            }
            admission_key = "bounded-learning-admission"
            trade_ids = []
            for index, pnl in enumerate((20.0, -200.0), start=1):
                episode_id = f"bounded-learning-episode-{index}"
                candidate = {
                    "venue": "OKX",
                    "inst_id": "BTC-USDT-SWAP",
                    "direction": "funding_capture_short_perp",
                    "trade_type": "perp_funding_basis",
                    "score": 70.0,
                    "last": 100.0,
                    "execution_feasibility": {"status": "standard"},
                    "signal_stats_scope": "direct",
                    "admission_key": admission_key,
                    "admission_episode_id": episode_id,
                    "episode_id": episode_id,
                }
                trade_id = open_paper_trade(conn, candidate, review, settings=legacy)
                trade_ids.append(trade_id)
                conn.execute(
                    """
                    update paper_trades
                    set status='closed',pnl_bps=?,close_measurement_status='valid'
                    where id=?
                    """,
                    (pnl, trade_id),
                )
                conn.execute(
                    """
                    insert into paper_admission_queue(
                        queue_id,admission_key,episode_id,evidence_fingerprint,
                        evidence_observed_at,lane,status,priority,venue,inst_id,
                        market_surface,lineage_root,direction,route_status,
                        candidate_json,eligibility_json,enqueued_at,updated_at,
                        paper_trade_id
                    ) values(?,?,?,?,?,'evidence','completed_valid',0,?,?,?,?,?,
                             'standard',?,'{}',?,?,?)
                    """,
                    (
                        f"queue-learning-{index}",
                        admission_key,
                        episode_id,
                        f"fingerprint-learning-{index}",
                        "2026-08-07T12:00:00+00:00",
                        candidate["venue"],
                        candidate["inst_id"],
                        candidate["trade_type"],
                        "OKX|perp_funding_basis",
                        candidate["direction"],
                        json.dumps(candidate, sort_keys=True),
                        "2026-08-07T12:00:00+00:00",
                        "2026-08-07T12:00:00+00:00",
                        trade_id if index == 1 else trade_id + 1000,
                    ),
                )
            conn.commit()

            bounded = copy.deepcopy(DEFAULT_SETTINGS)
            bounded["market_admission"]["enabled"] = True
            bounded["market_admission"]["paper_queue_enabled"] = True
            bounded["learning"]["task_emission_enabled"] = False
            bounded["learning"]["growth_experiment_emission_enabled"] = False
            stats = learning.update_signal_stats(conn, bounded)

            self.assertEqual(1, len(stats))
            only = next(iter(stats.values()))
            self.assertEqual(1, only["closed_count"])
            self.assertEqual(20.0, only["avg_pnl_bps"])
            self.assertEqual(0, conn.execute("select count(*) from improvement_tasks").fetchone()[0])
            self.assertEqual(0, conn.execute("select count(*) from growth_experiments").fetchone()[0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
