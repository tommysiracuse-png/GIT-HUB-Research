from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import self_improvement_open_pack as pack  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db, utc_now  # noqa: E402


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


class SelfImprovementOpenPackTests(unittest.TestCase):
    def test_report_builds_route_borrow_africa_kalshi_and_signal_diagnostics(self) -> None:
        conn = make_conn()
        conn.execute(
            """
            insert into signal_stats
                (signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional",
                9,
                1,
                -44.5,
                0.111,
                -10.0,
                utc_now(),
            ),
        )
        conn.execute(
            """
            insert into signal_stats
                (signal_key, closed_count, wins, avg_pnl_bps, win_rate, score_adjustment, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "OKX_SPOT|frontier_crypto_venue_map|long_frontier_spot|standard",
                12,
                8,
                18.0,
                0.667,
                5.0,
                utc_now(),
            ),
        )
        candidates = [
            {
                "venue": "GATE",
                "inst_id": "GATE:ARC_USDT",
                "direction": "short_frontier_spot",
                "trade_type": "frontier_crypto_venue_map",
                "edge_bps_estimate": 24.0,
                "estimated_round_trip_cost_bps": 20.0,
                "execution_route": {
                    "route_status": "conditional",
                    "missing_permissions": ["spot_borrow"],
                    "route_blockers": ["spot_borrow"],
                    "borrow_status": "required_unconfirmed",
                },
            }
        ]
        prediction_summary = {
            "by_venue": {"KALSHI": 11},
            "by_orderbook_status": {"verified": 8, "not_selected_for_depth": 3},
            "route_blockers": {
                "prediction_markets_account": 11,
                "venue_api_access": 11,
                "jurisdiction_eligibility": 11,
            },
        }

        report = pack.build_open_pack_report(
            conn,
            DEFAULT_SETTINGS,
            candidates=candidates,
            prediction_summary=prediction_summary,
            expansion_map={"frontier_crypto": {"observation_count": 568}},
        )

        self.assertTrue(report["paper_only"])
        self.assertFalse(report["live_trading_allowed"])
        borrow = report["route_borrow_intelligence"]
        self.assertEqual(borrow["record_count"], 1)
        self.assertEqual(borrow["shadow_only_unconfirmed_count"], 1)
        self.assertEqual(borrow["records"][0]["borrow_asset"], "ARC")
        africa = report["africa_rail_watchlist"]
        self.assertEqual(africa["venue_count"], 4)
        self.assertLessEqual(africa["instrument_count"], 16)
        kalshi = report["kalshi_public_coverage"]
        self.assertEqual(kalshi["current_candidate_count"], 11)
        self.assertEqual(kalshi["route_status"], "conditional")
        diagnostics = report["signal_repair_diagnostics"]
        self.assertEqual(diagnostics["active_loosenings_created"], 0)
        self.assertEqual(len(diagnostics["frontier_weak_signal_diagnostics"]), 2)
        self.assertEqual(len(diagnostics["positive_shadow_expansion_variants"]), 1)

    def test_cleanup_updates_statuses_without_deleting_rows(self) -> None:
        conn = make_conn()
        conn.execute(
            "insert into improvement_tasks (id, created_at, priority, title, rationale, status) values (?, ?, ?, ?, ?, ?)",
            (116738, utc_now(), 88, "LLM Resolve spot-borrow route blockers", "test", "open"),
        )
        conn.execute(
            "insert into growth_experiments (id, created_at, priority, signal_key, hypothesis, action, evidence_json, status) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (78527, utc_now(), 96, "OKX|perp_funding_basis", "OKX basis conditional short-spot failure", "diagnose", "{}", "open"),
        )
        conn.execute(
            "insert into growth_experiments (id, created_at, priority, signal_key, hypothesis, action, evidence_json, status) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (74464, utc_now(), 85, "OKX_SPOT|frontier", "OKX_SPOT short positive expectancy", "expand", "{}", "open"),
        )

        result = pack.close_covered_open_rows(conn)

        self.assertEqual(result["improvement_tasks_updated"], 1)
        self.assertEqual(result["growth_experiments_updated"], 1)
        self.assertEqual(result["growth_experiments_deduplicated"], 1)
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("select id, status from growth_experiments order by id").fetchall()
        }
        self.assertEqual(statuses[78527], pack.IMPLEMENTED_STATUS)
        self.assertEqual(statuses[74464], pack.DEDUPED_STATUS)
        self.assertEqual(conn.execute("select count(*) as n from growth_experiments").fetchone()["n"], 2)

    def test_duplicate_text_matches_open_pack_scope(self) -> None:
        self.assertTrue(pack.is_duplicate_open_pack_text("Add Kalshi read-only public event market coverage"))
        self.assertTrue(pack.is_duplicate_open_pack_text("Resolve spot borrow route data for frontier shorts"))
        self.assertFalse(pack.is_duplicate_open_pack_text("Build a new unrelated commodities adapter"))


if __name__ == "__main__":
    unittest.main()
