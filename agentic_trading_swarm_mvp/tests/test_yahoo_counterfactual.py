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
        attribution = report["diagnostic_attribution"]
        self.assertEqual(60, attribution["primary_horizon_minutes"])
        self.assertEqual("horizon_or_sign_mismatch", attribution["leading_hypothesis"])
        self.assertEqual(-32.0, attribution["forward_return_horizons"]["15"]["avg_net_pnl_bps"])
        self.assertEqual(18.0, attribution["forward_return_horizons"]["240"]["avg_net_pnl_bps"])
        self.assertEqual(-102.0, attribution["cost_summary"]["avg_net_pnl_bps"])
        self.assertEqual(-100.0, attribution["cost_summary"]["avg_gross_return_bps"])
        self.assertEqual(2.0, attribution["cost_summary"]["avg_total_realized_cost_bps"])
        self.assertEqual("long_proxy_standard", attribution["family_leg_outcomes"][0]["family_leg"])
        self.assertEqual("aging_15m_to_60m", attribution["quote_age_outcomes"][0]["quote_age_bucket"])
        self.assertEqual("friction_le_2bps", attribution["realized_cost_bucket_outcomes"][0]["realized_total_cost_bucket"])
        self.assertEqual("negative_before_cost", attribution["cost_drag_bucket_outcomes"][0]["cost_drag_bucket"])

    def test_cost_drag_hypothesis_is_confirmed_when_gross_turns_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(pathlib.Path(tmp) / "radar.sqlite")
            for index in range(10):
                candidate = {
                    "provider_age_seconds": 180.0,
                    "spread_bps": 4.0,
                    "estimated_slippage_bps": 6.0,
                    "seen_at": "2026-08-01T12:03:00+00:00",
                    "source_quote_timestamp": "2026-08-01T12:00:00+00:00",
                }
                cur = conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, venue, inst_id, direction, trade_type, signal_key,
                        base_score, learned_score, entry, status, thesis,
                        candidate_json, review_json, entry_fee_bps, entry_slippage_bps
                    ) values (?, 'YAHOO_PROXY', ?, 'long_proxy', 'global_proxy_momentum',
                              'YAHOO_PROXY|global_proxy_momentum|long_proxy|standard',
                              70, 70, 100, 'closed', 'test', ?, '{}', 3, 2)
                    """,
                    ("2026-08-01T12:03:00+00:00", f"COST{index}", json.dumps(candidate)),
                )
                trade_id = cur.lastrowid
                context = {
                    "paper_realized_cost_audit": {
                        "paper_only": True,
                        "charged_cost_bps": 5.0,
                        "realized_cost_backfill_bps": 7.0,
                        "adjusted_pnl_bps": -7.0,
                    }
                }
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status, delay_seconds
                    ) values (?, 60, '2026-08-01T13:03:00+00:00', 100.05, -7.0, ?, 'valid', 5)
                    """,
                    (trade_id, json.dumps(context)),
                )
            conn.commit()
            report = yahoo.build_report(conn)
            conn.close()

        attribution = report["diagnostic_attribution"]
        by_hypothesis = {item["hypothesis"]: item for item in attribution["hypothesis_tests"]}
        self.assertEqual("cost_drag", attribution["leading_hypothesis"])
        self.assertEqual("confirmed", by_hypothesis["cost_drag"]["status"])
        self.assertEqual(["long_proxy_standard"], by_hypothesis["cost_drag"]["affected_family_legs"])
        self.assertEqual(5.0, attribution["cost_summary"]["avg_gross_return_bps"])
        self.assertEqual(-7.0, attribution["cost_summary"]["avg_net_pnl_bps"])
        self.assertEqual(12.0, attribution["cost_summary"]["avg_total_realized_cost_bps"])
        self.assertEqual(4.0, attribution["cost_summary"]["avg_estimated_spread_bps"])
        self.assertEqual(6.0, attribution["cost_summary"]["avg_estimated_slippage_bps"])
        self.assertEqual(
            "moderate_8_to_16bps",
            attribution["realized_cost_bucket_outcomes"][0]["realized_total_cost_bucket"],
        )
        self.assertEqual(
            "cost_drag_flipped_negative",
            attribution["cost_drag_bucket_outcomes"][0]["cost_drag_bucket"],
        )

    def test_stale_proxy_hypothesis_is_confirmed_before_direction_flip_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(pathlib.Path(tmp) / "radar.sqlite")
            for prefix, age_seconds, price, pnl in (
                ("FRESH", 300.0, 100.08, 8.0),
                ("STALE", 7200.0, 99.8, -20.0),
            ):
                for index in range(8):
                    candidate = {
                        "provider_age_seconds": age_seconds,
                        "seen_at": "2026-08-01T12:00:00+00:00",
                        "source_quote_timestamp": "2026-08-01T11:00:00+00:00",
                    }
                    cur = conn.execute(
                        """
                        insert into paper_trades (
                            opened_at, venue, inst_id, direction, trade_type, signal_key,
                            base_score, learned_score, entry, status, thesis,
                            candidate_json, review_json, entry_fee_bps, entry_slippage_bps
                        ) values (?, 'YAHOO_PROXY', ?, 'long_proxy', 'global_proxy_momentum',
                                  'YAHOO_PROXY|global_proxy_momentum|long_proxy|standard',
                                  70, 70, 100, 'closed', 'test', ?, '{}', 0, 0)
                        """,
                        ("2026-08-01T12:00:00+00:00", f"{prefix}{index}", json.dumps(candidate)),
                    )
                    trade_id = cur.lastrowid
                    conn.execute(
                        """
                        insert into paper_trade_outcomes (
                            trade_id, horizon_minutes, measured_at, price, pnl_bps,
                            context_json, measurement_status, delay_seconds
                        ) values (?, 60, '2026-08-01T13:00:00+00:00', ?, ?, '{}', 'valid', 5)
                        """,
                        (trade_id, price, pnl),
                    )
            conn.commit()
            report = yahoo.build_report(conn)
            conn.close()

        attribution = report["diagnostic_attribution"]
        by_hypothesis = {item["hypothesis"]: item for item in attribution["hypothesis_tests"]}
        self.assertEqual("stale_proxy_data", attribution["leading_hypothesis"])
        self.assertEqual("confirmed", by_hypothesis["stale_proxy_data"]["status"])
        self.assertEqual("rejected", by_hypothesis["cost_drag"]["status"])
        self.assertEqual(1.0, by_hypothesis["stale_proxy_data"]["stale_negative_share"])
        quote_age = {row["quote_age_bucket"]: row for row in attribution["quote_age_outcomes"]}
        self.assertEqual(8.0, quote_age["fresh_le_15m"]["avg_net_pnl_bps"])
        self.assertEqual(-20.0, quote_age["stale_gt_60m"]["avg_net_pnl_bps"])

    def test_closed_trade_bucket_attribution_segments_realized_proxy_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(pathlib.Path(tmp) / "radar.sqlite")
            trades = [
                {
                    "inst_id": "FAST",
                    "direction": "long_proxy",
                    "entry": 100.0,
                    "exit": 100.12,
                    "pnl_bps": 12.0,
                    "selected_hold_minutes": 15,
                    "candidate": {
                        "provider_age_seconds": 300.0,
                        "spread_bps": 2.0,
                        "seen_at": "2026-08-01T08:30:00+00:00",
                        "source_quote_timestamp": "2026-08-01T08:25:00+00:00",
                        "source_session_status": "open",
                        "route_surface": "proxy",
                    },
                },
                {
                    "inst_id": "SLOW",
                    "direction": "short_proxy",
                    "entry": 100.0,
                    "exit": 99.82,
                    "pnl_bps": -18.0,
                    "selected_hold_minutes": 240,
                    "candidate": {
                        "provider_age_seconds": 7200.0,
                        "spread_bps": 18.0,
                        "seen_at": "2026-08-01T19:15:00+00:00",
                        "source_quote_timestamp": "2026-08-01T17:15:00+00:00",
                        "source_session_status": "closed",
                        "signal_stats_scope": "synthetic_research",
                        "paper_execution_semantics": "proxy_not_live_equivalent",
                        "paper_proxy_not_live_equivalent": True,
                        "route_surface": "cross_surface",
                    },
                },
            ]
            for trade in trades:
                candidate = trade["candidate"]
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type, signal_key,
                        base_score, learned_score, entry, exit, pnl_bps, status, thesis,
                        candidate_json, review_json, context_json, selected_hold_minutes
                    ) values (?, ?, 'YAHOO_PROXY', ?, ?, 'global_proxy_momentum', ?, 70, 70, ?, ?, ?, 'closed', 'test', ?, '{}', '{}', ?)
                    """,
                    (
                        candidate["seen_at"],
                        candidate["seen_at"],
                        trade["inst_id"],
                        trade["direction"],
                        f"YAHOO_PROXY|global_proxy_momentum|{trade['direction']}|standard",
                        trade["entry"],
                        trade["exit"],
                        trade["pnl_bps"],
                        json.dumps(candidate),
                        trade["selected_hold_minutes"],
                    ),
                )
                trade_id = conn.execute("select last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status, delay_seconds
                    ) values (?, 60, ?, ?, ?, '{}', 'valid', 5)
                    """,
                    (trade_id, candidate["seen_at"], trade["exit"], trade["pnl_bps"]),
                )
            conn.commit()

            report = yahoo.build_report(conn)
            conn.close()

        closed = report["diagnostic_attribution"]["closed_trade_bucket_attribution"]
        holding = {row["selected_holding_horizon_bucket"]: row for row in closed["selected_holding_horizon_outcomes"]}
        staleness = {row["quote_staleness_bucket"]: row for row in closed["quote_staleness_outcomes"]}
        session = {row["session_bucket"]: row for row in closed["session_outcomes"]}
        spread = {row["spread_regime_bucket"]: row for row in closed["spread_regime_outcomes"]}
        routing = {row["routing_path_bucket"]: row for row in closed["routing_path_outcomes"]}

        self.assertEqual(2, closed["closed_trade_count"])
        self.assertEqual(12.0, holding["scalp_le_15m"]["avg_pnl_bps"])
        self.assertEqual(-18.0, holding["swing_61m_to_240m"]["avg_pnl_bps"])
        self.assertEqual(12.0, staleness["fresh_le_15m"]["avg_pnl_bps"])
        self.assertEqual(-18.0, staleness["stale_gt_60m"]["avg_pnl_bps"])
        self.assertEqual(12.0, session["open"]["avg_pnl_bps"])
        self.assertEqual(-18.0, session["closed"]["avg_pnl_bps"])
        self.assertEqual(-18.0, spread["extreme_gt_15bps"]["avg_pnl_bps"])
        self.assertEqual(-18.0, routing["synthetic_proxy_not_live_equivalent"]["avg_pnl_bps"])

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
