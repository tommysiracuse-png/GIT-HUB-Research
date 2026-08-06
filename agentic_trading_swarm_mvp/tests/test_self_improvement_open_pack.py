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

    def test_yahoo_proxy_decay_analysis_localizes_stale_cross_surface_cohorts(self) -> None:
        conn = make_conn()
        trades = [
            ("ALPHA", "long_proxy", "proxy", 300.0, 2.0, "emea", "Europe/London", {60: 20.0, 240: 10.0}),
            ("BETA", "long_proxy", "proxy", 600.0, 3.0, "emea", "Europe/London", {60: 10.0, 240: 5.0}),
            ("GAMMA", "short_proxy", "cross_surface", 5400.0, 14.0, "apac", "Asia/Tokyo", {60: -40.0, 240: -30.0}),
            ("DELTA", "short_proxy", "cross_surface", 7200.0, 18.0, "apac", "Asia/Tokyo", {60: -50.0, 240: -35.0}),
        ]
        for symbol, direction, route_surface, provider_age, spread_bps, region, timezone, outcomes in trades:
            candidate = {
                "route_surface": route_surface,
                "provider_age_seconds": provider_age,
                "spread_bps": spread_bps,
                "region": region,
                "timezone": timezone,
            }
            cur = conn.execute(
                """
                insert into paper_trades (
                    opened_at, venue, inst_id, direction, trade_type, signal_key,
                    base_score, learned_score, entry, status, thesis,
                    candidate_json, review_json, context_json
                ) values (?, 'YAHOO_PROXY', ?, ?, 'global_proxy_momentum', ?, 70, 70, 100, 'closed', 'test', ?, '{}', ?)
                """,
                (
                    utc_now(),
                    symbol,
                    direction,
                    f"YAHOO_PROXY|global_proxy_momentum|{direction}|standard",
                    json.dumps(candidate),
                    json.dumps({"route_surface": route_surface, "region": region, "timezone": timezone}),
                ),
            )
            trade_id = cur.lastrowid
            for horizon, pnl in outcomes.items():
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status, delay_seconds
                    ) values (?, ?, ?, 100, ?, '{}', 'valid', 5)
                    """,
                    (trade_id, horizon, utc_now(), pnl),
                )
        conn.commit()

        report = pack.build_open_pack_report(conn, DEFAULT_SETTINGS)
        analysis = report["signal_repair_diagnostics"]["yahoo_proxy_decay_analysis"]

        self.assertEqual(8, analysis["reliable_label_count"])
        self.assertEqual(4, analysis["unique_trade_count"])
        self.assertEqual(60, analysis["primary_horizon_minutes"])
        self.assertEqual(15.0, analysis["direction_horizon_curves"]["long_proxy"]["60"]["avg_pnl_bps"])
        self.assertEqual(-45.0, analysis["direction_horizon_curves"]["short_proxy"]["60"]["avg_pnl_bps"])
        self.assertEqual("cross_surface", analysis["route_surface_outcomes"][0]["route_surface"])
        self.assertEqual(-45.0, analysis["route_surface_outcomes"][0]["avg_pnl_bps"])
        self.assertEqual("apac|Asia/Tokyo", analysis["proxy_cohort_outcomes"][0]["proxy_cohort"])
        self.assertEqual("stale_gt_60m", analysis["signal_age_outcomes"][0]["signal_age_bucket"])
        self.assertTrue(analysis["localization_summary"]["localized_decay_detected"])
        self.assertIn("route_surface_mismatch", analysis["localization_summary"]["likely_decay_sources"])
        self.assertIn("stale_proxy_concentration", analysis["localization_summary"]["likely_decay_sources"])
        self.assertIn("regional_time_zone_cohort_mismatch", analysis["localization_summary"]["likely_decay_sources"])
        self.assertIn("directional_asymmetry", analysis["localization_summary"]["likely_decay_sources"])

    def test_yahoo_proxy_decay_analysis_exposes_horizon_and_cost_buckets(self) -> None:
        conn = make_conn()
        for index in range(10):
            candidate = {
                "route_surface": "proxy",
                "provider_age_seconds": 120.0,
                "spread_bps": 4.0,
                "region": "emea",
                "timezone": "Europe/London",
                "seen_at": "2026-08-01T12:03:00+00:00",
                "source_quote_timestamp": "2026-08-01T12:00:00+00:00",
            }
            cur = conn.execute(
                """
                insert into paper_trades (
                    opened_at, venue, inst_id, direction, trade_type, signal_key,
                    base_score, learned_score, entry, status, thesis,
                    candidate_json, review_json, context_json, entry_fee_bps, entry_slippage_bps
                ) values (?, 'YAHOO_PROXY', ?, 'long_proxy', 'global_proxy_momentum',
                          'YAHOO_PROXY|global_proxy_momentum|long_proxy|standard',
                          70, 70, 100, 'closed', 'test', ?, '{}', '{}', 3, 2)
                """,
                ("2026-08-01T12:03:00+00:00", f"COST{index}", json.dumps(candidate)),
            )
            trade_id = cur.lastrowid
            for horizon, price, pnl, context in (
                (15, 99.75, -25.0, "{}"),
                (
                    60,
                    100.05,
                    -7.0,
                    json.dumps(
                        {
                            "paper_realized_cost_audit": {
                                "paper_only": True,
                                "charged_cost_bps": 5.0,
                                "realized_cost_backfill_bps": 7.0,
                            }
                        }
                    ),
                ),
                (240, 100.14, 14.0, "{}"),
            ):
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status, delay_seconds
                    ) values (?, ?, ?, ?, ?, ?, 'valid', 5)
                    """,
                    (trade_id, horizon, utc_now(), price, pnl, context),
                )
        conn.commit()

        report = pack.build_open_pack_report(conn, DEFAULT_SETTINGS)
        analysis = report["signal_repair_diagnostics"]["yahoo_proxy_decay_analysis"]
        by_hypothesis = {
            item["hypothesis"]: item
            for item in analysis["counterfactual_hypothesis_tests"]
        }

        self.assertEqual(30, analysis["reliable_label_count"])
        self.assertEqual(10, analysis["unique_trade_count"])
        self.assertEqual(60, analysis["primary_horizon_minutes"])
        self.assertEqual(-25.0, analysis["forward_return_horizons"]["15"]["avg_net_pnl_bps"])
        self.assertEqual(-7.0, analysis["forward_return_horizons"]["60"]["avg_net_pnl_bps"])
        self.assertEqual(14.0, analysis["forward_return_horizons"]["240"]["avg_net_pnl_bps"])
        self.assertEqual("cost_drag", analysis["leading_counterfactual_hypothesis"])
        self.assertEqual("confirmed", by_hypothesis["cost_drag"]["status"])
        self.assertEqual("confirmed", by_hypothesis["horizon_or_sign_mismatch"]["status"])
        self.assertEqual(12.0, analysis["counterfactual_cost_summary"]["avg_total_realized_cost_bps"])
        self.assertEqual(
            "moderate_8_to_16bps",
            analysis["realized_cost_bucket_outcomes"][0]["realized_total_cost_bucket"],
        )
        self.assertEqual(
            "cost_drag_flipped_negative",
            analysis["cost_drag_bucket_outcomes"][0]["cost_drag_bucket"],
        )

    def test_yahoo_proxy_decay_analysis_surfaces_bounded_hypothesis_labels_and_realized_window(self) -> None:
        conn = make_conn()
        trades = [
            ("P1", "long_proxy", "proxy", 300.0, 2.0, 0.92, 6.0, {5: 8.0, 15: 12.0, 60: 4.0}),
            ("P2", "long_proxy", "proxy", 600.0, 4.0, 0.78, 4.0, {5: 6.0, 15: 10.0, 60: 2.0}),
            ("X1", "short_proxy", "cross_surface", 5400.0, 12.0, 0.25, -8.0, {5: -10.0, 15: -14.0, 60: -20.0}),
            ("X2", "short_proxy", "cross_surface", 7200.0, 18.0, 0.18, -6.0, {5: -8.0, 15: -12.0, 60: -18.0}),
        ]
        for symbol, direction, route_surface, provider_age, spread_bps, liquidity_score, realized_pnl, outcomes in trades:
            candidate = {
                "route_surface": route_surface,
                "provider_age_seconds": provider_age,
                "spread_bps": spread_bps,
                "liquidity_score": liquidity_score,
                "region": "emea" if route_surface == "proxy" else "apac",
                "timezone": "Europe/London" if route_surface == "proxy" else "Asia/Tokyo",
            }
            cur = conn.execute(
                """
                insert into paper_trades (
                    opened_at, closed_at, venue, inst_id, direction, trade_type, signal_key,
                    base_score, learned_score, entry, exit, pnl_bps, status, thesis,
                    candidate_json, review_json, context_json
                ) values (?, ?, 'YAHOO_PROXY', ?, ?, 'global_proxy_momentum', ?, 70, 70, 100, 101, ?, 'closed', 'test', ?, '{}', ?)
                """,
                (
                    utc_now(),
                    utc_now(),
                    symbol,
                    direction,
                    f"YAHOO_PROXY|global_proxy_momentum|{direction}|standard",
                    realized_pnl,
                    json.dumps(candidate),
                    json.dumps({"route_surface": route_surface}),
                ),
            )
            trade_id = cur.lastrowid
            for horizon, pnl in outcomes.items():
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, measurement_status, delay_seconds
                    ) values (?, ?, ?, 100, ?, '{}', 'valid', 5)
                    """,
                    (trade_id, horizon, utc_now(), pnl),
                )
        conn.commit()

        report = pack.build_open_pack_report(conn, DEFAULT_SETTINGS)
        analysis = report["signal_repair_diagnostics"]["yahoo_proxy_decay_analysis"]
        labels = analysis["bounded_hypothesis_labels"]
        five_minute = labels["windows"]["5m"]
        realized = labels["windows"]["realized_post_entry"]
        markdown = pack.render_open_pack_markdown(report)

        self.assertEqual(["5m", "15m", "60m", "realized_post_entry"], labels["tracked_windows"])
        self.assertEqual("stale_gt_60m", analysis["signal_freshness_outcomes"][0]["signal_age_bucket"])
        self.assertEqual("low", analysis["liquidity_bucket_outcomes"][0]["liquidity_bucket"])
        self.assertEqual(4, five_minute["overall"]["count"])
        self.assertEqual(-9.0, five_minute["route_surface_outcomes"][0]["avg_pnl_bps"])
        self.assertEqual("cross_surface", five_minute["route_surface_outcomes"][0]["route_surface"])
        self.assertEqual("low", realized["entry_liquidity_bucket_outcomes"][0]["liquidity_bucket"])
        self.assertEqual(-7.0, realized["route_surface_outcomes"][0]["avg_pnl_bps"])
        self.assertIn("Yahoo bounded hypothesis windows", markdown)
        self.assertIn("Yahoo realized_post_entry failure labels", markdown)

    def test_duplicate_text_matches_open_pack_scope(self) -> None:
        self.assertTrue(pack.is_duplicate_open_pack_text("Add Kalshi read-only public event market coverage"))
        self.assertTrue(pack.is_duplicate_open_pack_text("Resolve spot borrow route data for frontier shorts"))
        self.assertFalse(pack.is_duplicate_open_pack_text("Build a new unrelated commodities adapter"))


if __name__ == "__main__":
    unittest.main()
