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
        signal_key = "DIRECT|BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional"
        conn.executemany(
            """
            insert into paper_trades (
                opened_at, venue, inst_id, direction, trade_type, signal_key,
                base_score, learned_score, entry, status, thesis, candidate_json,
                review_json, pnl_bps, close_measurement_status
            ) values (datetime('now'), 'BITGET', 'BITGET:BTC-USDT', 'short_frontier_spot',
                      'frontier_crypto_venue_map', ?, 70.0, 70.0, 1.0, 'closed',
                      'sample calibration', '{}', '{}', 4.0, 'valid')
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

    def test_learning_excludes_late_and_legacy_close_measurements(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        signal_key = "OKX|perp_funding_basis|funding_capture_short_perp|standard"
        conn.executemany(
            """
            insert into paper_trades (
                opened_at, closed_at, venue, inst_id, direction, trade_type,
                signal_key, base_score, learned_score, entry, pnl_bps, status,
                thesis, candidate_json, review_json, context_json,
                close_measurement_status
            ) values (
                datetime('now'), datetime('now'), 'OKX', 'BTC-USDT-SWAP',
                'funding_capture_short_perp', 'perp_funding_basis', ?,
                70.0, 70.0, 1.0, ?, 'closed', 'measurement quality', '{}',
                '{}', '{}', ?
            )
            """,
            [
                (signal_key, -25.0, "valid"),
                (signal_key, 250.0, "late"),
                (signal_key, 500.0, None),
            ],
        )

        with (
            mock.patch.object(learning, "generate_improvement_tasks"),
            mock.patch.object(learning, "generate_growth_experiments"),
            mock.patch.object(learning, "write_backlog"),
            mock.patch.object(learning, "write_growth_plan"),
        ):
            stats = learning.update_signal_stats(conn, copy.deepcopy(DEFAULT_SETTINGS))

        self.assertEqual(1, stats[signal_key]["closed_count"])
        self.assertEqual(-25.0, stats[signal_key]["avg_pnl_bps"])

    def test_learning_excludes_synthetic_scope_and_legacy_synthetic_signal_keys(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.executemany(
            """
            insert into paper_trades (
                opened_at, closed_at, venue, inst_id, direction, trade_type,
                signal_key, base_score, learned_score, entry, pnl_bps, status,
                thesis, candidate_json, review_json, context_json,
                close_measurement_status
            ) values (
                datetime('now'), datetime('now'), 'OKX', 'BTC-USDT-SWAP',
                'long', 'test', ?, 70.0, 70.0, 1.0, 100.0, 'closed',
                'scope isolation', '{}', '{}', ?, 'valid'
            )
            """,
            [
                ("SYNTHETIC_RESEARCH|legacy", "{}"),
                ("DIRECT|mislabelled", '{"signal_stats_scope":"synthetic_research"}'),
            ],
        )

        with (
            mock.patch.object(learning, "generate_improvement_tasks"),
            mock.patch.object(learning, "generate_growth_experiments"),
            mock.patch.object(learning, "write_backlog"),
            mock.patch.object(learning, "write_growth_plan"),
        ):
            stats = learning.update_signal_stats(conn, copy.deepcopy(DEFAULT_SETTINGS))

        self.assertEqual({}, stats)


if __name__ == "__main__":
    unittest.main()
