from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import contextual_failure_filters  # noqa: E402
import market_admission  # noqa: E402
import self_improvement  # noqa: E402
from paper_context_drag import context_drag_statistics, context_identifier  # noqa: E402
from storage import connect  # noqa: E402


class ReliableCloseMeasurementConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.signal_key = "TARGET|frontier_crypto_venue_map|long_frontier_spot|standard"
        self.candidate = {
            "venue": "TARGET",
            "inst_id": "TARGET:ABC_USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "source_venue": "REFERENCE",
            "gross_edge_bps_estimate": 20.0,
            "entry_slippage_bps_estimate": 3.0,
            "spread_bps": 8.0,
            "liquidity_score": 0.7,
            "latency_ms": 800,
            "local_short_horizon_trend_bps": -5.0,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_trade(self, pnl_bps: float, measurement_status: str | None) -> int:
        cursor = self.conn.execute(
            """
            insert into paper_trades (
                opened_at, closed_at, venue, inst_id, direction, trade_type,
                signal_key, base_score, learned_score, entry, exit, pnl_bps,
                status, thesis, candidate_json, review_json, context_json,
                close_measurement_status
            ) values (
                '2026-08-06T12:00:00+00:00', '2026-08-06T13:00:00+00:00',
                'TARGET', 'TARGET:ABC_USDT', 'long_frontier_spot',
                'frontier_crypto_venue_map', ?, 80, 80, 100, 101, ?,
                'closed', 'measurement reliability test', ?, '{}', '{}', ?
            )
            """,
            (self.signal_key, pnl_bps, json.dumps(self.candidate), measurement_status),
        )
        return int(cursor.lastrowid)

    def test_learning_and_context_consumers_only_use_valid_closes(self) -> None:
        self._insert_trade(10.0, "valid")
        self._insert_trade(500.0, "late")
        self._insert_trade(-500.0, "missing")
        self._insert_trade(900.0, None)

        signal_metrics = self_improvement._closed_metrics_since(self.conn, self.signal_key)
        overall_metrics = self_improvement._overall_metrics(self.conn)
        contextual_rows = contextual_failure_filters._closed_trade_rows(self.conn)
        drag_stats = context_drag_statistics(self.conn)

        self.assertEqual(1, signal_metrics["closed_count"])
        self.assertEqual(10.0, signal_metrics["avg_pnl_bps"])
        self.assertEqual(1, overall_metrics["closed_count"])
        self.assertEqual([10.0], [row["pnl_bps"] for row in contextual_rows])
        self.assertEqual(1, drag_stats[context_identifier(self.candidate)]["closed_count"])
        self.assertEqual(10.0, drag_stats[context_identifier(self.candidate)]["avg_realized_pnl_bps"])

    def test_market_admission_counts_only_reliable_route_eligible_labels(self) -> None:
        valid_id = self._insert_trade(12.0, "valid")
        late_id = self._insert_trade(800.0, "late")
        for trade_id in (valid_id, late_id):
            for horizon in (30, 60):
                self.conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status
                    ) values (?, ?, '2026-08-06T13:00:00+00:00', 101, 12, '{}', 'valid')
                    """,
                    (trade_id, horizon),
                )

        stats = market_admission._paper_stats(self.conn)[("TARGET:ABC_USDT", self.signal_key)]

        self.assertEqual(2, stats["trades"])
        self.assertEqual(1, stats["closed_trades"])
        self.assertEqual(2, stats["valid_labels"])
        self.assertEqual(12.0, stats["avg_pnl_bps"])

    def test_market_admission_partial_schema_is_conservative(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                create table paper_trades (
                    id integer primary key, inst_id text, signal_key text, status text,
                    pnl_bps real, candidate_json text, review_json text, context_json text
                );
                create table paper_trade_outcomes (
                    trade_id integer, measurement_status text
                );
                insert into paper_trades values (
                    1, 'TARGET:ABC_USDT', 'legacy', 'closed', 999, '{}', '{}', '{}'
                );
                insert into paper_trade_outcomes values (1, 'valid');
                """
            )

            stats = market_admission._paper_stats(conn)[("TARGET:ABC_USDT", "legacy")]

            self.assertEqual(1, stats["trades"])
            self.assertEqual(0, stats["closed_trades"])
            self.assertEqual(0, stats["valid_labels"])
            self.assertIsNone(stats["avg_pnl_bps"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
