from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_admission_bridge
from settings import DEFAULT_SETTINGS
from storage import init_db


def state(stage: str, **overrides):
    value = {
        "admission_key": f"key-{stage}",
        "venue": "BITKUB",
        "inst_id": "BITKUB:BTC_THB",
        "market_surface": "regional_spot",
        "strategy_lineage": "adapter_observation",
        "current_stage": stage,
        "highest_stage": stage,
        "blocker_code": None,
        "session_status": "open",
        "details": {"quality_status": "verified", "route_status": "standard", "valid_labels": 0},
    }
    value.update(overrides)
    return value


class MarketAdmissionBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_json = market_admission_bridge.REPORT_JSON
        self.old_md = market_admission_bridge.REPORT_MD
        market_admission_bridge.REPORT_JSON = pathlib.Path(self.temp.name) / "bridge.json"
        market_admission_bridge.REPORT_MD = pathlib.Path(self.temp.name) / "bridge.md"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)

    def tearDown(self) -> None:
        self.conn.close()
        market_admission_bridge.REPORT_JSON = self.old_json
        market_admission_bridge.REPORT_MD = self.old_md
        self.temp.cleanup()

    def test_priceable_market_creates_one_canonical_enrichment_directive(self) -> None:
        report = {"states": [state("priceable", blocker_code="quality_unverified")]}
        first = market_admission_bridge.run_market_admission_bridge(self.conn, self.settings, report)
        second = market_admission_bridge.run_market_admission_bridge(self.conn, self.settings, report)
        self.assertEqual(1, first["summary"]["actions_created"])
        self.assertEqual(1, second["summary"]["duplicates_suppressed"])
        self.assertEqual(1, self.conn.execute("select count(*) from market_hunter_directives").fetchone()[0])

    def test_quality_verified_observation_creates_strategy_lab_discovery(self) -> None:
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [state("quality_verified")]}
        )
        self.assertEqual(1, result["summary"]["actions_created"])
        row = self.conn.execute("select * from strategy_lab_experiments").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("admission_discovery_", row["strategy_lab_id"])

    def test_spot_borrow_user_constraint_suppresses_route_task(self) -> None:
        item = state(
            "strategy_candidate",
            strategy_lineage="frontier_crypto|short_frontier_spot",
            blocker_code="route_spot_borrow",
        )
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [item]}
        )
        self.assertEqual(1, result["summary"]["user_constraints_suppressed"])
        self.assertEqual(0, self.conn.execute("select count(*) from route_probe_tasks").fetchone()[0])

    def test_closed_market_creates_no_action(self) -> None:
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn,
            self.settings,
            {"states": [state("quality_verified", session_status="closed", blocker_code="market_closed")]},
        )
        self.assertEqual([], result["actions"])

    def test_advancing_stage_resolves_prior_enrichment_topic(self) -> None:
        first_state = state("priceable", blocker_code="quality_unverified")
        market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [first_state]}
        )
        advanced = state("quality_verified", admission_key=first_state["admission_key"], blocker_code=None)
        result = market_admission_bridge.run_market_admission_bridge(
            self.conn, self.settings, {"states": [advanced]}
        )
        self.assertEqual(1, result["summary"]["prior_stage_topics_resolved"])
        status = self.conn.execute("select status from market_hunter_directives").fetchone()[0]
        self.assertEqual("resolved_market_admission_advanced", status)


if __name__ == "__main__":
    unittest.main()
