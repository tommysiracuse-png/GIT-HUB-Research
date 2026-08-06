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
from paper_order_router import FRONTIER_PAPER_ADMISSION_REASON_PREFIX  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import execution_summary, init_db, open_paper_trade, record_due_horizon_outcomes  # noqa: E402
import strategy_reliability  # noqa: E402


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

    def test_frontier_ticket_carries_shadow_learning_metadata(self) -> None:
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:ICP-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "last": 10.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": ["simulated_slippage_exceeds_edge"],
            "edge_bps_estimate": 0.0,
            "gross_edge_bps_estimate": 12.898,
            "estimated_round_trip_cost_bps": 20.092,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        review = {
            "paper_allocation_multiplier": 1.0,
            "net_edge_bps_estimate": 0.0,
            "feasibility_status": "standard",
            "route_status": "standard",
        }

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual("shadow_excluded_from_learning", ticket["paper_label_exclusion_reason"])
        self.assertTrue(ticket["paper_shadow_excluded_from_learning"])
        self.assertEqual(
            [
                "simulated_slippage_exceeds_edge",
                "net_edge_after_round_trip_cost_not_positive",
            ],
            ticket["paper_shadow_exclusion_triggers"],
        )
        self.assertEqual(["simulated_slippage_exceeds_edge"], ticket["anomaly_flags"])
        self.assertEqual(0.0, ticket["net_edge_bps_estimate"])

    def test_order_ticket_carries_okx_basis_context_gate_reason(self) -> None:
        candidate = {
            "venue": "OKX",
            "inst_id": "OKX:BTC-USDT-SWAP",
            "direction": "long_perp_short_spot",
            "trade_type": "perp_funding_basis",
            "last": 100.0,
            "paper_context_gate_reason": "okx_reverse_basis_conditional_route_cap",
            "paper_context_gate_action": "cap_conditional_reverse_basis",
            "paper_context_gate_promotion_eligible": False,
            "paper_context_gate_paper_fill_allowed": True,
        }
        review = {
            "paper_allocation_multiplier": 1.0,
            "feasibility_status": "conditional",
            "route_status": "conditional",
        }

        ticket = build_order_ticket(candidate, review, DEFAULT_SETTINGS)

        self.assertEqual("okx_reverse_basis_conditional_route_cap", ticket["paper_context_gate_reason"])
        self.assertEqual("cap_conditional_reverse_basis", ticket["paper_context_gate_action"])
        self.assertFalse(ticket["paper_context_gate_promotion_eligible"])
        self.assertTrue(ticket["paper_context_gate_paper_fill_allowed"])

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
            "frontier_paper_admission_guard_applies": True,
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
        self.assertEqual("short_frontier_spot_spot_borrow_blocked", row["reject_reason"])
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

    def test_frontier_fill_gate_uses_bounded_net_edge_reason(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "COINBASE",
            "inst_id": "COINBASE:ETH-USD",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "frontier_paper_admission_guard_applies": True,
            "last": 100.0,
            "edge_bps_estimate": 0.0,
            "gross_edge_bps_estimate": 20.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": [],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 0.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("net_edge_floor_failed", execution["candidate"]["candidate_reject_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("net_edge_floor_failed", observation["reject_reason"])

    def test_frontier_fill_gate_uses_bounded_shadow_only_quality_reason(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:STRK-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "frontier_paper_admission_guard_applies": True,
            "last": 1.0,
            "score": 95.4,
            "edge_bps_estimate": 16.0,
            "gross_edge_bps_estimate": 30.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "degraded",
            "quality_action": "shadow_only",
            "anomaly_flags": ["depth_cliff"],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 16.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("shadow_only_quality_gate", execution["candidate"]["candidate_reject_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("shadow_only_quality_gate", observation["reject_reason"])

    def test_frontier_fill_gate_persists_specific_shadow_reason_for_invalid_level_value(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "OKX_SPOT:ICP-USDT",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "market_surface": "frontier_crypto_venue_map",
            "last": 1.0,
            "score": 88.0,
            "edge_bps_estimate": 16.0,
            "gross_edge_bps_estimate": 30.0,
            "estimated_round_trip_cost_bps": 20.0,
            "quality_status": "verified",
            "quality_action": "normal",
            "anomaly_flags": ["invalid_level_value"],
        }
        review = {"paper_allocation_multiplier": 1.0, "net_edge_bps_estimate": 16.0}

        execution = execute_order(conn, candidate, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("net_edge_floor_failed", execution["candidate"]["candidate_reject_reason"])
        self.assertEqual("invalid_level_value", execution["candidate"]["shadow_reason"])
        observation = conn.execute(
            "select reject_reason from frontier_paper_shadow_observations"
        ).fetchone()
        self.assertEqual("invalid_level_value", observation["reject_reason"])

    def test_yahoo_proxy_freshness_shadow_only_candidate_opens_synthetic_research_trade(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        candidate = {
            "seen_at": "2026-08-06T14:19:00+00:00",
            "venue": "YAHOO_PROXY",
            "inst_id": "YAHOO_PROXY:EWZ",
            "direction": "long_proxy",
            "trade_type": "global_proxy_momentum",
            "last": 100.0,
            "score": 88.0,
            "spread_bps": 2.0,
            "liquidity_score": 0.8,
            "change_24h_pct": 1.2,
            "edge_bps_estimate": 10.0,
            "basis_bps": 0.0,
            "funding_bps": 0.0,
            "source_quote_timestamp": "2026-08-06T14:00:00+00:00",
            "source_session_status": "closed",
            "source_session_open": False,
            "source_quote_age_seconds": 1140.0,
            "last_trade_timestamp": "2026-08-06T14:00:00+00:00",
            "last_trade_age_seconds": 1140.0,
            "pre_entry_tick_returns_bps": [-18.0, -10.0, 5.0, -6.0],
            "proxy_reuse_gate": {
                "quote_age_seconds": 1140.0,
                "source_session_status": "closed",
                "reasons": ["opening_gap_without_live_followthrough"],
            },
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
        }
        candidate, _ = strategy_reliability.apply_strategy_reliability([candidate], {"mode": "paper"})
        reviewed = candidate[0]
        review = {
            "decision": "approve_paper_trade",
            "signal_key": reviewed["inst_id"],
            "learned_score": reviewed["score"],
            "confidence": 0.8,
            "net_edge_bps_estimate": 10.0,
            "paper_allocation_multiplier": 1.0,
            "feasibility_status": "standard",
            "route_status": "standard",
            "missing_requirements": [],
        }

        execution = execute_order(conn, reviewed, review, DEFAULT_SETTINGS)

        self.assertFalse(execution["paper_filled"])
        self.assertTrue(execution["paper_observation_ready"])
        self.assertEqual("shadow_only", execution["order"]["status"])
        self.assertEqual("synthetic_research", execution["order"]["signal_stats_scope"])

        trade_id = open_paper_trade(conn, reviewed, review, execution=execution, settings=DEFAULT_SETTINGS)
        row = conn.execute(
            "select status, context_json from paper_trades where id = ?",
            (trade_id,),
        ).fetchone()
        context = json.loads(row["context_json"])
        self.assertEqual("open", row["status"])
        self.assertEqual("synthetic_research", context["signal_stats_scope"])

        observed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=61)
        conn.execute("update paper_trades set opened_at = ? where id = ?", (observed_at.isoformat(), trade_id))
        conn.commit()
        recorded = record_due_horizon_outcomes(
            conn,
            {
                "YAHOO_PROXY:EWZ": {
                    "last": 101.0,
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            },
            {"learning": {"horizon_minutes": [60], "max_outcome_delay_seconds": 300}},
        )
        self.assertEqual(1, len(recorded))
        outcome = conn.execute(
            "select context_json from paper_trade_outcomes where trade_id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual("synthetic_research", json.loads(outcome["context_json"])["signal_stats_scope"])


if __name__ == "__main__":
    unittest.main()
