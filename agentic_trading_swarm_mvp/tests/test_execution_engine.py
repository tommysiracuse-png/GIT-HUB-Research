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

from execution_engine import execute_order  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402


class ExecutionEnginePaperGuardTests(unittest.TestCase):
    def test_unconfirmed_frontier_spot_borrow_creates_shadow_ticket_no_fill(self) -> None:
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
            "quality_action": "verified",
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
        self.assertEqual(result["order"]["status"], "shadow_filtered")
        self.assertEqual(result["fills"], [])
        row = conn.execute("select status, order_json, candidate_json from execution_orders").fetchone()
        self.assertEqual(row["status"], "shadow_filtered")
        self.assertIn("spot_borrow_unconfirmed", row["order_json"])
        saved_candidate = json.loads(row["candidate_json"])
        self.assertTrue(saved_candidate["shadow_filtered"])


if __name__ == "__main__":
    unittest.main()
