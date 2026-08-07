from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_loop  # noqa: E402


class LegacyPaperLoopMeasurementIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        paper_loop.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_trade(
        self,
        inst_id: str,
        *,
        pnl_bps: float | None = None,
        measurement_status: str = "legacy_unverified",
        snapshot: dict | None = None,
        status: str = "open",
    ) -> int:
        cursor = self.conn.execute(
            """insert into paper_trades(
                opened_at, closed_at, venue, inst_id, direction, score, entry,
                exit, pnl_bps, status, thesis, snapshot_json,
                close_measurement_status
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2026-08-06T12:00:00+00:00",
                "2026-08-06T13:00:00+00:00" if status == "closed" else None,
                "OKX",
                inst_id,
                "long_perp_short_spot",
                80.0,
                100.0,
                101.0 if status == "closed" else None,
                pnl_bps,
                status,
                "measurement test",
                json.dumps(snapshot or {"execution_feasibility": {"status": "standard"}}),
                measurement_status,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def test_close_records_timely_measurement_provenance(self) -> None:
        trade_id = self._insert_trade("BTC-USDT-SWAP")
        now = dt.datetime(2026, 8, 6, 13, 1, tzinfo=dt.timezone.utc)
        latest = {
            "inst_id": "BTC-USDT-SWAP",
            "last": 101.0,
            "venue": "OKX",
            "seen_at": "2026-08-06T13:00:30+00:00",
            "signal_age_seconds": 10.0,
        }

        with mock.patch.object(paper_loop, "utc_now", return_value=now):
            closed = paper_loop.close_due_trades(
                self.conn,
                {"BTC-USDT-SWAP": latest},
                hold_minutes=60,
                max_delay_seconds=60,
            )

        row = self.conn.execute(
            """select target_close_at, close_observed_at, close_delay_seconds,
                      close_measurement_status, close_price_source
               from paper_trades where id=?""",
            (trade_id,),
        ).fetchone()
        self.assertEqual(closed[0]["measurement_status"], "valid")
        self.assertEqual(row[0], "2026-08-06T13:00:00+00:00")
        self.assertEqual(row[1], "2026-08-06T13:00:20+00:00")
        self.assertEqual(row[2], 20.0)
        self.assertEqual(row[3], "valid")
        self.assertEqual(row[4], "OKX")

    def test_price_observed_before_target_does_not_close_trade(self) -> None:
        trade_id = self._insert_trade("ETH-USDT-SWAP")
        now = dt.datetime(2026, 8, 6, 13, 5, tzinfo=dt.timezone.utc)
        latest = {
            "inst_id": "ETH-USDT-SWAP",
            "last": 101.0,
            "venue": "OKX",
            "seen_at": "2026-08-06T13:00:30+00:00",
            "signal_age_seconds": 60.0,
        }

        with mock.patch.object(paper_loop, "utc_now", return_value=now):
            closed = paper_loop.close_due_trades(
                self.conn,
                {"ETH-USDT-SWAP": latest},
                hold_minutes=60,
            )

        self.assertEqual(closed, [])
        status = self.conn.execute("select status from paper_trades where id=?", (trade_id,)).fetchone()[0]
        self.assertEqual(status, "open")

    def test_summary_excludes_late_and_route_blocked_closes_but_keeps_proxy(self) -> None:
        self._insert_trade("DIRECT", pnl_bps=10.0, measurement_status="valid", status="closed")
        self._insert_trade("LATE", pnl_bps=1000.0, measurement_status="late", status="closed")
        self._insert_trade(
            "BLOCKED",
            pnl_bps=500.0,
            measurement_status="valid",
            status="closed",
            snapshot={
                "execution_feasibility": {
                    "status": "conditional",
                    "missing_requirements": ["spot_borrow"],
                }
            },
        )
        self._insert_trade(
            "PROXY",
            pnl_bps=20.0,
            measurement_status="valid",
            status="closed",
            snapshot={
                "signal_key": "PAPER_PROXY|OKX|BTC",
                "execution_feasibility": {"status": "standard"},
            },
        )

        summary = paper_loop.performance_summary(self.conn)

        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["unreliable_closed"], 2)
        self.assertEqual(summary["avg_pnl_bps"], 15.0)
        self.assertEqual(summary["win_rate"], 1.0)

    def test_init_migrates_legacy_table_to_unverified_measurements(self) -> None:
        legacy = sqlite3.connect(":memory:")
        try:
            legacy.execute(
                """create table paper_trades(
                    id integer primary key autoincrement, opened_at text not null,
                    closed_at text, venue text not null, inst_id text not null,
                    direction text not null, score real not null, entry real not null,
                    exit real, pnl_bps real, status text not null, thesis text not null,
                    snapshot_json text not null
                )"""
            )
            legacy.execute(
                """insert into paper_trades(
                    opened_at, closed_at, venue, inst_id, direction, score, entry,
                    exit, pnl_bps, status, thesis, snapshot_json
                ) values('2026-08-06T12:00:00+00:00','2026-08-06T13:00:00+00:00',
                    'OKX','LEGACY','long_perp_short_spot',80,100,101,100,'closed','legacy','{}')"""
            )

            paper_loop.init_db(legacy)

            measurement_status = legacy.execute(
                "select close_measurement_status from paper_trades"
            ).fetchone()[0]
            self.assertEqual(measurement_status, "legacy_unverified")
            self.assertEqual(paper_loop.performance_summary(legacy)["closed"], 0)
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
