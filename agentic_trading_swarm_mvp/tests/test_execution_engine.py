from __future__ import annotations

import json
import datetime as dt
import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import build_order_ticket, execute_order  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import execution_summary, init_db, record_due_horizon_outcomes  # noqa: E402


class ExecutionEnginePaperGuardTests(unittest.TestCase):
    def test_route_requirement_report_sizes_paper_ticket_without_blocking_it(self) -> None:
        candidate = {
            "venue": "CME_GROUP",
            "inst_id": "CME_GROUP:PROXY",
            "direction": "short_proxy",
            "trade_type": "global_market_discovery_proxy",
            "last": 10.0,
            "paper_route_requirement_report": {
                "paper_only": True,
                "read_only": True,
                "applies": True,
                "paper_allocation_multiplier": 0.6,
                "hard_blocking": False,
            },
        }
        review = {"paper_allocation_multiplier": 1.0}

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual(600.0, ticket["notional_usd"])
        self.assertEqual("ready_for_paper_execution", ticket["status"])

    def test_unconfirmed_frontier_spot_borrow_is_shadow_observed_without_an_order(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "GATE",
            "inst_id": "GATE:ARC_USDT",
            "direction": "short_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "signal_key": "GATE|frontier_crypto_venue_map|short_frontier_spot|conditional",
            "last": 1.0,
            "score": 80.0,
            "edge_bps_estimate": 24.0,
            "gross_edge_bps_estimate": 60.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "execution_route": {
                "route_id": "conditional_crypto_route_paper",
                "route_status": "conditional",
                "missing_permissions": ["spot_borrow"],
                "route_blockers": ["spot_borrow"],
                "borrow_status": "required_unconfirmed",
            },
        }
        review = {
            "decision": "approve_conditional_paper_trade",
            "confidence": 0.8,
            "net_edge_bps_estimate": 24.0,
            "feasibility_status": "conditional",
            "route_status": "conditional",
            "missing_requirements": ["spot_borrow"],
            "paper_allocation_multiplier": 1.0,
        }

        result = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(result["paper_filled"])
        self.assertEqual(result["order"]["status"], "shadow_only")
        self.assertEqual([], result["fills"])
        self.assertIsNone(result["order_id"])
        self.assertEqual(0, conn.execute("select count(*) from execution_orders").fetchone()[0])
        row = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("paper_net_edge_guard", row["reject_reason"])
        counters = execution_summary(conn)["frontier_paper_candidates"]
        self.assertEqual(0, counters["accepted"])
        self.assertEqual(1, counters["shadowed"])

        observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)
        conn.execute(
            "update frontier_paper_shadow_observations set observed_at = ?",
            (observed_at.isoformat(),),
        )
        outcomes = record_due_horizon_outcomes(
            conn,
            {"GATE:ARC_USDT": {"last": 1.01, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}},
            {"learning": {"horizon_minutes": [60], "max_outcome_delay_seconds": 300}},
        )
        self.assertEqual(1, len(outcomes))
        self.assertIn("shadow_observation_id", outcomes[0])
        self.assertEqual(1, conn.execute("select count(*) from frontier_paper_shadow_outcomes").fetchone()[0])
        self.assertEqual(0, conn.execute("select count(*) from paper_trade_outcomes").fetchone()[0])

    def test_execution_summary_counts_accepted_frontier_fill(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:BTC-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 100.0,
            "edge_bps_estimate": 12.0,
            "gross_edge_bps_estimate": 35.0,
            "estimated_round_trip_cost_bps": 20.0,
            "anomaly_flags": [],
            "quality_status": "verified",
            "quality_action": "normal",
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 12.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertTrue(execution["paper_filled"])
        self.assertEqual(15.0, execution["candidate"]["frontier_net_edge_bps"])
        counters = execution_summary(conn)["frontier_paper_candidates"]
        self.assertEqual(1, counters["accepted"])
        self.assertEqual(0, counters["shadowed"])


if __name__ == "__main__":
    unittest.main()
