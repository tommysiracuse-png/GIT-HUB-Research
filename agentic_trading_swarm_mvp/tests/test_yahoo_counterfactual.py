from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import yahoo_counterfactual as yahoo
from storage import connect


class YahooCounterfactualTests(unittest.TestCase):
    def test_reliable_labels_drive_direction_freshness_and_horizon_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(pathlib.Path(tmp) / "radar.sqlite")
            for index in range(12):
                candidate = {
                    "stale_minutes": 20,
                    "last_bar_utc": "2026-08-01T12:00:00+00:00",
                    "seen_at": "2026-08-01T12:20:00+00:00",
                }
                cur = conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, venue, inst_id, direction, trade_type, signal_key,
                        base_score, learned_score, entry, status, thesis,
                        candidate_json, review_json, entry_fee_bps, entry_slippage_bps
                    ) values (?, 'YAHOO_PROXY', ?, 'long_proxy', 'global_proxy_momentum',
                              'YAHOO_PROXY|global_proxy_momentum|long_proxy|standard',
                              70, 70, 100, 'closed', 'test', ?, '{}', 1, 1)
                    """,
                    ("2026-08-01T12:20:00+00:00", f"TEST{index}", json.dumps(candidate)),
                )
                trade_id = cur.lastrowid
                for horizon, price, pnl in ((15, 99.7, -32.0), (60, 99.0, -102.0), (240, 100.2, 18.0)):
                    conn.execute(
                        """
                        insert into paper_trade_outcomes (
                            trade_id, horizon_minutes, measured_at, price, pnl_bps,
                            context_json, measurement_status, delay_seconds
                        ) values (?, ?, '2026-08-01T13:20:00+00:00', ?, ?, '{}', 'valid', 10)
                        """,
                        (trade_id, horizon, price, pnl),
                    )
            conn.commit()

            old_runs = yahoo.RUNS_DIR
            old_json = yahoo.REPORT_JSON
            old_md = yahoo.REPORT_MD
            yahoo.RUNS_DIR = pathlib.Path(tmp)
            yahoo.REPORT_JSON = pathlib.Path(tmp) / "report.json"
            yahoo.REPORT_MD = pathlib.Path(tmp) / "report.md"
            try:
                report = yahoo.run_yahoo_counterfactual_analysis(conn)
            finally:
                yahoo.RUNS_DIR = old_runs
                yahoo.REPORT_JSON = old_json
                yahoo.REPORT_MD = old_md
                conn.close()

        self.assertEqual(report["horizon_metrics"]["60"]["avg_pnl_bps"], -102.0)
        self.assertEqual(report["counterfactuals"]["direction_flip_60m"]["avg_pnl_bps"], 98.0)
        self.assertEqual(report["counterfactuals"]["freshness_gates_60m"]["le_30m"]["count"], 12)
        self.assertEqual(report["counterfactuals"]["next_session_entry"]["status"], "forward_observation_required")
        self.assertTrue(any(item["counterfactual"] == "direction_flip_60m" for item in report["shadow_recommendations"]))

    def test_late_and_strategy_lab_labels_are_excluded(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            create table paper_trades (
                id integer primary key, opened_at text, venue text, inst_id text,
                direction text, trade_type text, entry real, entry_fee_bps real,
                entry_slippage_bps real, candidate_json text, strategy_lab_id text
            );
            create table paper_trade_outcomes (
                trade_id integer, horizon_minutes integer, price real, pnl_bps real,
                delay_seconds real, measurement_status text
            );
            """
        )
        conn.execute(
            "insert into paper_trades values (1, '', 'YAHOO_PROXY', 'A', 'long_proxy', 'global_proxy_momentum', 100, 0, 0, '{}', null)"
        )
        conn.execute("insert into paper_trade_outcomes values (1, 60, 101, 100, 600, 'late')")
        conn.execute(
            "insert into paper_trades values (2, '', 'YAHOO_PROXY', 'B', 'long_proxy', 'global_proxy_momentum', 100, 0, 0, '{}', 'lab')"
        )
        conn.execute("insert into paper_trade_outcomes values (2, 60, 101, 100, 10, 'valid')")

        report = yahoo.build_report(conn)
        conn.close()

        self.assertEqual(report["reliable_label_count"], 0)
        self.assertEqual(report["decision"], "diagnose_only_no_positive_counterfactual")


if __name__ == "__main__":
    unittest.main()
