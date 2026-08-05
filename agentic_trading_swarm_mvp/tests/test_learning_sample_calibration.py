import copy
import sqlite3
import unittest
from unittest import mock

from src import learning
from src.settings import DEFAULT_SETTINGS
from src.storage import init_db


class LearningSampleCalibrationTests(unittest.TestCase):
    def test_conditional_frontier_short_requires_stable_sample_for_adjustment(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        signal_key = "SYNTHETIC_RESEARCH|BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional"
        conn.executemany(
            """
            insert into paper_trades (
                opened_at, venue, inst_id, direction, trade_type, signal_key,
                base_score, learned_score, entry, status, thesis, candidate_json,
                review_json, pnl_bps
            ) values (datetime('now'), 'BITGET', 'BITGET:BTC-USDT', 'short_frontier_spot',
                      'frontier_crypto_venue_map', ?, 70.0, 70.0, 1.0, 'closed',
                      'sample calibration', '{}', '{}', 4.0)
            """,
            [(signal_key,)] * 5,
        )
        conn.commit()

        with (
            mock.patch.object(learning, "generate_improvement_tasks"),
            mock.patch.object(learning, "generate_growth_experiments"),
            mock.patch.object(learning, "write_backlog"),
            mock.patch.object(learning, "write_growth_plan"),
        ):
            stats = learning.update_signal_stats(conn, copy.deepcopy(DEFAULT_SETTINGS))

        self.assertEqual(stats[signal_key]["closed_count"], 5)
        self.assertEqual(stats[signal_key]["score_adjustment"], 0.0)
        self.assertEqual(stats[signal_key]["score_adjustment_min_samples"], 20)
        self.assertEqual(stats[signal_key]["score_adjustment_sample_confidence"], 0.25)


if __name__ == "__main__":
    unittest.main()
