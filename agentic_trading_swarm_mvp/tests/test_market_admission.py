import copy
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

import market_admission
from settings import DEFAULT_SETTINGS
from storage import init_db


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def settings():
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg["market_admission"] = {
        "enabled": True,
        "consecutive_failures_degraded": 2,
        "diagnostic_after_eligible_scans": 2,
        "implementation_task_after_eligible_scans": 3,
        "requested_symbols": ["EWY"],
    }
    return cfg


def global_candidate(**overrides):
    item = {
        "venue": "KOREA_EXCHANGE",
        "inst_id": "KOREA_EXCHANGE:EWY",
        "proxy_symbol": "EWY",
        "proxy_surface": "country_etf",
        "market_surface": "global_market_discovery",
        "trade_type": "global_market_discovery_proxy",
        "direction": "long_proxy",
        "last": 74.0,
        "score": 60.0,
        "edge_bps_estimate": 12.0,
        "liquidity_score": 0.8,
        "spread_bps": 2.0,
        "stale_minutes": 2.0,
        "session_status": "open",
        "proxy_quality_status": "verified_proxy",
        "data_status": "reachable",
        "signal_lineage_key": "GLOBAL_ACTIVE|country_etf|country_adr_relative_momentum_v1",
        "execution_feasibility": {"status": "standard"},
        "data_source": {"provider": "Yahoo market data"},
    }
    item.update(overrides)
    return item


class MarketAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_json = market_admission.REPORT_JSON
        self.old_md = market_admission.REPORT_MD
        market_admission.REPORT_JSON = pathlib.Path(self.temp.name) / "admission.json"
        market_admission.REPORT_MD = pathlib.Path(self.temp.name) / "admission.md"

    def tearDown(self):
        market_admission.REPORT_JSON = self.old_json
        market_admission.REPORT_MD = self.old_md
        self.temp.cleanup()

    def test_tracks_paper_eligible_global_strategy_separately(self):
        with memory_db() as conn:
            candidate = global_candidate()
            review = {"decision": "approve_paper_trade", "hard_blocks": []}
            report = market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [candidate],
                [{"candidate": candidate, "review": review}],
            )
            state = report["states"][0]
            self.assertEqual("paper_eligible", state["current_stage"])
            self.assertEqual("healthy", state["health_status"])
            self.assertIn("country_adr_relative_momentum_v1", state["strategy_lineage"])

    def test_bybit_403_is_network_state_not_strategy_failure(self):
        observation = {
            "venue": "BYBIT_SPOT",
            "instrument_id": "BYBIT_SPOT:ALL",
            "market_type": "spot",
            "data_status": "blocked",
            "http_status": "HTTP 403: Forbidden",
            "access_blocker_code": "network_region_blocked",
            "session_status": "continuous",
            "source_url": "https://api.bybit.com/v5/market/tickers?category=spot",
        }
        with memory_db() as conn:
            report = None
            for _ in range(2):
                report = market_admission.run_market_admission_monitor(conn, settings(), [], [], [observation])
            state = report["states"][0]
            self.assertEqual("network_region_blocked", state["blocker_code"])
            self.assertEqual("blocked", state["health_status"])
            self.assertEqual("adapter_observation", state["strategy_lineage"])

    def test_stall_creates_one_task_and_progress_resolves_it(self):
        stalled = global_candidate(direction="watch_only", candidate_reject_reason="surface_confirmation_missing")
        with memory_db() as conn:
            for _ in range(4):
                market_admission.run_market_admission_monitor(conn, settings(), [stalled], [], [])
            tasks = conn.execute(
                "select id, status from improvement_tasks where title like 'Market admission cohort stalled [%]'"
            ).fetchall()
            directives = conn.execute(
                "select id, status from market_hunter_directives "
                "where market_key like 'market_admission_cohort|%'"
            ).fetchall()
            self.assertEqual(1, len(tasks))
            self.assertEqual("open", tasks[0]["status"])
            self.assertEqual(1, len(directives))
            self.assertEqual("open", directives[0]["status"])

            healthy = global_candidate()
            review = {"decision": "approve_paper_trade", "hard_blocks": []}
            market_admission.run_market_admission_monitor(
                conn,
                settings(),
                [healthy],
                [{"candidate": healthy, "review": review}],
                [],
            )
            status = conn.execute("select status from improvement_tasks where id = ?", (tasks[0]["id"],)).fetchone()["status"]
            directive_status = conn.execute(
                "select status from market_hunter_directives where id = ?",
                (directives[0]["id"],),
            ).fetchone()["status"]
            self.assertEqual("resolved_market_admission_advanced", status)
            self.assertEqual("resolved_market_admission_advanced", directive_status)

    def test_report_is_machine_readable(self):
        with memory_db() as conn:
            market_admission.run_market_admission_monitor(conn, settings(), [global_candidate()], [], [])
        payload = json.loads(market_admission.REPORT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["summary"]["requested_symbols_observed"])

    def test_legacy_per_instrument_stall_tasks_are_superseded_by_cohort_model(self):
        with memory_db() as conn:
            for idx in range(3):
                conn.execute(
                    """
                    insert into improvement_tasks (created_at, priority, title, rationale, status)
                    values ('now', 95, ?, 'OKX normalization_missing', 'open')
                    """,
                    (f"Market admission stalled [legacy-{idx}]",),
                )
            conn.commit()
            report = market_admission.run_market_admission_monitor(conn, settings(), [], [], [])
            statuses = [row["status"] for row in conn.execute("select status from improvement_tasks")]
        self.assertEqual(["superseded_by_market_admission_cohort"] * 3, statuses)
        self.assertEqual(3, report["summary"]["task_cohorts"]["legacy_instrument_tasks_superseded"])


if __name__ == "__main__":
    unittest.main()
