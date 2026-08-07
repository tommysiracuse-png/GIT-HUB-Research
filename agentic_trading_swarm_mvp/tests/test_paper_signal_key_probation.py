import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signal_safety import _closed_metrics


class PaperSignalKeyProbationMetricsTests(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            create table paper_trades (
                signal_key text,
                status text,
                pnl_bps text,
                closed_at text
            )
            """
        )
        return conn

    def test_closed_metrics_ignores_non_finite_and_invalid_pnl_values(self) -> None:
        conn = self._conn()
        rows = [
            ("loser_key", "closed", "10", "2026-07-18T00:00:00+00:00"),
            ("loser_key", "closed", "nan", "2026-07-18T00:01:00+00:00"),
            ("loser_key", "closed", "inf", "2026-07-18T00:02:00+00:00"),
            ("loser_key", "closed", "-20", "2026-07-18T00:03:00+00:00"),
            ("loser_key", "closed", "not-a-number", "2026-07-18T00:04:00+00:00"),
            ("loser_key", "open", "999", "2026-07-18T00:05:00+00:00"),
        ]
        conn.executemany("insert into paper_trades values (?, ?, ?, ?)", rows)

        metrics = _closed_metrics(conn, "loser_key")

        self.assertEqual(metrics["closed_count"], 2)
        self.assertEqual(metrics["avg_pnl_bps"], -5.0)
        self.assertEqual(metrics["win_rate"], 0.5)
        self.assertEqual(metrics["best_bps"], 10.0)
        self.assertEqual(metrics["worst_bps"], -20.0)

    def test_closed_metrics_returns_empty_summary_when_no_finite_closed_pnls_exist(self) -> None:
        conn = self._conn()
        rows = [
            ("bad_key", "closed", "nan", "2026-07-18T00:00:00+00:00"),
            ("bad_key", "closed", "inf", "2026-07-18T00:01:00+00:00"),
            ("bad_key", "closed", "-inf", "2026-07-18T00:02:00+00:00"),
            ("bad_key", "closed", "oops", "2026-07-18T00:03:00+00:00"),
        ]
        conn.executemany("insert into paper_trades values (?, ?, ?, ?)", rows)

        metrics = _closed_metrics(conn, "bad_key")

        self.assertEqual(
            metrics,
            {
                "closed_count": 0,
                "avg_pnl_bps": None,
                "win_rate": None,
                "best_bps": None,
                "worst_bps": None,
            },
        )

    def test_recent_window_is_applied_after_reliable_label_filtering(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            create table paper_trades (
                signal_key text, status text, pnl_bps text, closed_at text,
                candidate_json text, review_json text, context_json text,
                close_measurement_status text
            )
            """
        )
        conn.executemany(
            "insert into paper_trades values (?,?,?,?,?,?,?,?)",
            [
                (
                    "window_key", "closed", str(pnl), timestamp,
                    "{}", "{}", "{}", measurement,
                )
                for pnl, timestamp, measurement in (
                    (10, "2026-07-18T00:00:00+00:00", "valid"),
                    (20, "2026-07-18T00:01:00+00:00", "valid"),
                    (999, "2026-07-18T00:12:00+00:00", "late"),
                    (999, "2026-07-18T00:11:00+00:00", "missing"),
                    (999, "2026-07-18T00:10:00+00:00", "late"),
                )
            ],
        )

        metrics = _closed_metrics(conn, "window_key", limit=2)

        self.assertEqual(2, metrics["closed_count"])
        self.assertEqual(15.0, metrics["avg_pnl_bps"])


if __name__ == "__main__":
    unittest.main()
