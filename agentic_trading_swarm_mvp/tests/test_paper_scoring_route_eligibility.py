from __future__ import annotations

import copy
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

import learning  # noqa: E402
import strategy_reliability as sr  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import (  # noqa: E402
    UNRESOLVED_ROUTE_REQUIREMENT_EXCLUSION_REASON,
    init_db,
    open_paper_trade,
    performance_summary,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_closed_trade(
    conn: sqlite3.Connection,
    *,
    signal_key: str,
    pnl_bps: float,
    candidate: dict,
    review: dict,
    context: dict | None = None,
) -> None:
    conn.execute(
        """
        insert into paper_trades (
            opened_at, closed_at, venue, inst_id, direction, trade_type, signal_key,
            base_score, learned_score, entry, exit, pnl_bps, status, thesis,
            candidate_json, review_json, context_json
        ) values (
            '2026-08-06T10:00:00+00:00', '2026-08-06T11:00:00+00:00', ?, ?, ?, ?, ?,
            80.0, 80.0, 100.0, 101.0, ?, 'closed', 'test',
            ?, ?, ?
        )
        """,
        (
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate["trade_type"],
            signal_key,
            pnl_bps,
            json.dumps(candidate, sort_keys=True),
            json.dumps(review, sort_keys=True),
            json.dumps(context or {}, sort_keys=True),
        ),
    )


class PaperScoringRouteEligibilityTests(unittest.TestCase):
    def test_open_paper_trade_persists_unresolved_route_label_exclusion(self) -> None:
        conn = make_conn()
        try:
            candidate = {
                "venue": "BITSO",
                "inst_id": "BITSO:XRP_MXN",
                "direction": "short_frontier_spot",
                "trade_type": "frontier_crypto_venue_map",
                "score": 82.0,
                "last": 100.0,
                "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                "execution_route": {
                    "route_id": "conditional_crypto_route_paper",
                    "route_status": "conditional",
                    "route_blockers": ["spot_borrow"],
                },
            }
            review = {
                "learned_score": 82.0,
                "feasibility_status": "conditional",
                "route_status": "conditional",
                "effective_route_id": "conditional_crypto_route_paper",
                "missing_requirements": ["spot_borrow"],
            }

            trade_id = open_paper_trade(conn, candidate, review, settings=DEFAULT_SETTINGS)
            row = conn.execute(
                "select context_json from paper_trades where id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            conn.close()

        context = json.loads(row["context_json"])
        self.assertFalse(context["paper_label_eligible"])
        self.assertEqual(
            UNRESOLVED_ROUTE_REQUIREMENT_EXCLUSION_REASON,
            context["paper_label_exclusion_reason"],
        )
        self.assertEqual(["spot_borrow"], context["paper_label_route_blockers"])
        self.assertTrue(context["paper_shadow_observation"])

    def test_learning_excludes_unresolved_conditional_labels_but_keeps_overrides(self) -> None:
        conn = make_conn()
        signal_key = "TEST|frontier_crypto_venue_map|short_frontier_spot|mixed"
        try:
            standard_candidate = {
                "venue": "OKX_SPOT",
                "inst_id": "OKX_SPOT:BTC-USDT",
                "direction": "short_frontier_spot",
                "trade_type": "frontier_crypto_venue_map",
                "execution_feasibility": {"status": "standard", "route_status": "standard"},
            }
            standard_review = {
                "feasibility_status": "standard",
                "route_status": "standard",
                "effective_route_id": "generic_paper_route",
            }
            insert_closed_trade(
                conn,
                signal_key=signal_key,
                pnl_bps=10.0,
                candidate=standard_candidate,
                review=standard_review,
                context={"route_status": "standard", "route_id": "generic_paper_route"},
            )

            blocked_candidate = {
                "venue": "BITGET",
                "inst_id": "BITGET:BTC-USDT",
                "direction": "short_frontier_spot",
                "trade_type": "frontier_crypto_venue_map",
                "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                "execution_route": {
                    "route_id": "conditional_crypto_route_paper",
                    "route_status": "conditional",
                    "route_blockers": ["spot_borrow"],
                },
            }
            blocked_review = {
                "feasibility_status": "conditional",
                "route_status": "conditional",
                "effective_route_id": "conditional_crypto_route_paper",
                "missing_requirements": ["spot_borrow"],
            }
            insert_closed_trade(
                conn,
                signal_key=signal_key,
                pnl_bps=-30.0,
                candidate=blocked_candidate,
                review=blocked_review,
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "route_blockers": ["spot_borrow"],
                },
            )

            override_candidate = copy.deepcopy(blocked_candidate)
            override_candidate["paper_label_eligible"] = True
            override_review = dict(blocked_review, paper_label_eligible=True)
            insert_closed_trade(
                conn,
                signal_key=signal_key,
                pnl_bps=-5.0,
                candidate=override_candidate,
                review=override_review,
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "route_blockers": ["spot_borrow"],
                    "paper_label_eligible": True,
                },
            )

            empty_blocker_candidate = copy.deepcopy(blocked_candidate)
            empty_blocker_candidate["execution_route"]["route_blockers"] = []
            empty_blocker_review = dict(blocked_review, missing_requirements=[])
            insert_closed_trade(
                conn,
                signal_key=signal_key,
                pnl_bps=20.0,
                candidate=empty_blocker_candidate,
                review=empty_blocker_review,
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "route_blockers": [],
                },
            )
            conn.commit()

            with (
                mock.patch.object(learning, "generate_improvement_tasks"),
                mock.patch.object(learning, "generate_growth_experiments"),
                mock.patch.object(learning, "write_backlog"),
                mock.patch.object(learning, "write_growth_plan"),
            ):
                stats = learning.update_signal_stats(conn, copy.deepcopy(DEFAULT_SETTINGS))
        finally:
            conn.close()

        self.assertEqual(3, stats[signal_key]["closed_count"])
        self.assertEqual(8.333, stats[signal_key]["avg_pnl_bps"])
        self.assertEqual(0.667, stats[signal_key]["win_rate"])

    def test_performance_summary_separates_unresolved_route_shadow_pnl_by_blocker(self) -> None:
        conn = make_conn()
        try:
            insert_closed_trade(
                conn,
                signal_key="COINBASE|frontier_crypto_venue_map|long_frontier_spot|standard",
                pnl_bps=5.0,
                candidate={
                    "venue": "COINBASE",
                    "inst_id": "COINBASE:BTC-USD",
                    "direction": "long_frontier_spot",
                    "trade_type": "frontier_crypto_venue_map",
                    "execution_feasibility": {"status": "standard", "route_status": "standard"},
                },
                review={
                    "feasibility_status": "standard",
                    "route_status": "standard",
                    "effective_route_id": "generic_paper_route",
                },
                context={"route_status": "standard", "route_id": "generic_paper_route"},
            )
            insert_closed_trade(
                conn,
                signal_key="BITGET|frontier_crypto_venue_map|short_frontier_spot|conditional",
                pnl_bps=-25.0,
                candidate={
                    "venue": "BITGET",
                    "inst_id": "BITGET:BTC-USDT",
                    "direction": "short_frontier_spot",
                    "trade_type": "frontier_crypto_venue_map",
                    "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                },
                review={
                    "feasibility_status": "conditional",
                    "route_status": "conditional",
                    "effective_route_id": "conditional_crypto_route_paper",
                    "missing_requirements": ["spot_borrow"],
                },
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "route_blockers": ["spot_borrow"],
                },
            )
            insert_closed_trade(
                conn,
                signal_key="KALSHI|prediction_market|buy_yes_event|conditional",
                pnl_bps=-15.0,
                candidate={
                    "venue": "KALSHI",
                    "inst_id": "KALSHI:ELECTION",
                    "direction": "buy_yes_event",
                    "trade_type": "prediction_market",
                    "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
                },
                review={
                    "feasibility_status": "conditional",
                    "route_status": "conditional",
                    "effective_route_id": "conditional_prediction_route_paper",
                    "missing_requirements": ["prediction_markets_account"],
                },
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_prediction_route_paper",
                    "route_blockers": ["prediction_markets_account"],
                },
            )
            conn.commit()
            summary = performance_summary(conn)
        finally:
            conn.close()

        self.assertEqual(1, summary["closed"])
        self.assertEqual(5.0, summary["avg_pnl_bps"])
        shadow = summary["unresolved_route_requirement_shadow"]
        self.assertEqual(2, shadow["closed_count"])
        self.assertEqual(-20.0, shadow["avg_pnl_bps"])
        self.assertEqual(-40.0, shadow["total_pnl_bps"])
        self.assertEqual(1, shadow["by_blocker"]["spot_borrow"]["closed_count"])
        self.assertEqual(
            1,
            shadow["by_blocker"]["prediction_markets_account"]["closed_count"],
        )

    def test_strategy_reliability_realized_context_ignores_unresolved_conditional_shadow_labels(self) -> None:
        conn = make_conn()
        candidate = {
            "score": 60.0,
            "venue": "OKX",
            "inst_id": "OKX:BTC-USDT",
            "market_surface": "spot",
            "direction": "long_frontier_spot",
            "trade_type": "frontier_crypto_venue_map",
            "last": 100.0,
            "liquidity_score": 0.8,
            "execution_feasibility": {"status": "conditional", "route_status": "conditional"},
        }
        try:
            eligible_candidate = {
                **candidate,
                "paper_label_eligible": True,
            }
            eligible_review = {
                "feasibility_status": "conditional",
                "route_status": "conditional",
                "effective_route_id": "conditional_crypto_route_paper",
                "paper_label_eligible": True,
            }
            insert_closed_trade(
                conn,
                signal_key="OKX_SPOT|frontier_crypto_venue_map|long_frontier_spot|conditional",
                pnl_bps=15.0,
                candidate=eligible_candidate,
                review=eligible_review,
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "paper_label_eligible": True,
                },
            )
            blocked_candidate = dict(candidate)
            blocked_review = {
                "feasibility_status": "conditional",
                "route_status": "conditional",
                "effective_route_id": "conditional_crypto_route_paper",
                "missing_requirements": ["spot_borrow"],
            }
            insert_closed_trade(
                conn,
                signal_key="OKX_SPOT|frontier_crypto_venue_map|long_frontier_spot|conditional",
                pnl_bps=-30.0,
                candidate=blocked_candidate,
                review=blocked_review,
                context={
                    "route_status": "conditional",
                    "route_id": "conditional_crypto_route_paper",
                    "route_blockers": ["spot_borrow"],
                },
            )
            conn.commit()

            config = {
                "mode": "paper",
                "allow_live_trading": False,
                "paper_context_priors": {
                    "realized_context_min_closed_trades": 1,
                    "realized_context_window_closed_trades": 10,
                    "realized_context_positive_scale": 0.2,
                    "realized_context_negative_scale": 0.3,
                    "top_rank_min_closed_trades": 1,
                    "top_rank_min_avg_pnl_bps": -100.0,
                    "conditional_rank_score_cap": 100.0,
                },
            }
            sr.hydrate_paper_context_prior_statistics([candidate], conn, config)
            detail = sr.apply_paper_context_priors(candidate, config)
        finally:
            conn.close()

        assert detail is not None
        self.assertEqual(1, detail["realized_context_closed_count"])
        self.assertEqual(15.0, detail["realized_context_avg_pnl_bps"])


if __name__ == "__main__":
    unittest.main()
