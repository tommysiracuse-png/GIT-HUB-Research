import copy
import datetime as dt
import json
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import llm_swarm_runner
from settings import DEFAULT_SETTINGS
from storage import init_db, open_paper_trade, save_opportunity, signal_key
from strategy_lab import (
    evaluate_strategy_lab,
    generate_strategy_lab_candidates,
    ingest_strategy_lab_recommendation,
    strategy_lab_summary,
)


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def base_settings():
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["allow_live_trading"] = False
    settings["strategy_lab"]["promote_min_active_hours"] = 48.0
    settings["strategy_lab"]["promote_min_labels"] = 30
    settings["strategy_lab"]["consecutive_passes_to_promote"] = 2
    return settings


def lab_rec():
    return {
        "recommendation_id": "rec_lab_1",
        "payload": {
            "action": "propose_strategy_lab_experiment",
            "priority": "high",
            "title": "Test OKX spot survivor continuation",
            "rationale": "Invent a tracked sub-strategy from strong OKX spot candidates.",
            "agent_name": "strategy_lab",
            "strategy_lab_experiment": {
                "strategy_lab_id": "okx_spot_survivor_lab_v1",
                "hypothesis": "High-quality OKX spot frontier longs continue after dislocation.",
                "strategy_logic": {
                    "type": "candidate_filter",
                    "venues": ["OKX_SPOT"],
                    "directions": ["long_frontier_spot"],
                    "trade_types": ["frontier_crypto_venue_map"],
                    "min_edge_bps": 10,
                    "min_liquidity_score": 0.35,
                    "max_spread_bps": 8,
                },
                "data_requirements": {"required_fields": ["edge_bps_estimate"]},
                "risk_gates": {"min_edge_bps": 10},
                "promotion_rules": {"promote_min_labels": 30},
            },
        },
    }


def candidate(**overrides):
    row = {
        "venue": "OKX_SPOT",
        "inst_id": "NEAR-USDT",
        "direction": "long_frontier_spot",
        "trade_type": "frontier_crypto_venue_map",
        "score": 70.0,
        "liquidity_score": 0.75,
        "spread_bps": 2.0,
        "last": 3.5,
        "edge_bps_estimate": 18.0,
        "change_24h_pct": 1.0,
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "execution_feasibility": {"status": "standard"},
    }
    row.update(overrides)
    return row


class StrategyLabTest(unittest.TestCase):
    def test_swarm_contains_strategy_lab_between_researcher_and_red_team(self):
        names = [agent["name"] for agent in llm_swarm_runner.AGENTS]
        self.assertIn("strategy_lab", names)
        self.assertLess(names.index("cross_market_researcher"), names.index("strategy_lab"))
        self.assertLess(names.index("strategy_lab"), names.index("red_team"))

    def test_ingests_strategy_lab_recommendation_as_active_experiment(self):
        with memory_db() as conn:
            result = ingest_strategy_lab_recommendation(conn, lab_rec())
            self.assertEqual("created", result[0]["action_status"])

            row = conn.execute(
                "select strategy_lab_id, status, hypothesis from strategy_lab_experiments"
            ).fetchone()
            self.assertEqual("okx_spot_survivor_lab_v1", row["strategy_lab_id"])
            self.assertEqual("active_testing", row["status"])
            self.assertIn("OKX spot", row["hypothesis"])

    def test_candidate_generation_emits_standard_candidate_with_lab_id(self):
        settings = base_settings()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            generated, report = generate_strategy_lab_candidates(conn, settings, [candidate()])

            self.assertEqual(1, len(generated))
            self.assertEqual(1, report["generated_candidates"])
            self.assertEqual("okx_spot_survivor_lab_v1", generated[0]["strategy_lab_id"])
            self.assertEqual("frontier_crypto_venue_map", generated[0]["trade_type"])
            self.assertEqual("long_frontier_spot", generated[0]["direction"])
            self.assertGreater(generated[0]["score"], 70.0)
            self.assertTrue(signal_key(generated[0]).startswith("STRATEGY_LAB|okx_spot_survivor_lab_v1|"))

    def test_lab_id_persists_through_opportunity_and_paper_trade(self):
        settings = base_settings()
        with memory_db() as conn:
            lab_candidate = candidate(strategy_lab_id="lab_persist_v1", strategy_lab_version=1)
            review = {
                "learned_score": 71.0,
                "decision": "approve_paper_trade",
                "route_status": "standard",
                "hard_blocks": [],
            }
            save_opportunity(conn, lab_candidate, review)
            trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)

            opportunity = conn.execute("select strategy_lab_id from opportunities").fetchone()
            trade = conn.execute("select strategy_lab_id, signal_key from paper_trades where id = ?", (trade_id,)).fetchone()
            self.assertEqual("lab_persist_v1", opportunity["strategy_lab_id"])
            self.assertEqual("lab_persist_v1", trade["strategy_lab_id"])
            self.assertTrue(trade["signal_key"].startswith("STRATEGY_LAB|lab_persist_v1|"))

    def test_evaluator_queues_code_change_after_second_promotion_pass(self):
        settings = base_settings()
        created_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            conn.execute(
                """
                update strategy_lab_experiments
                set created_at = ?, consecutive_passes = 1
                where strategy_lab_id = ?
                """,
                (created_at, "okx_spot_survivor_lab_v1"),
            )
            for idx in range(30):
                lab_candidate = candidate(
                    inst_id=f"NEAR-{idx}",
                    strategy_lab_id="okx_spot_survivor_lab_v1",
                    strategy_lab_version=1,
                )
                review = {
                    "learned_score": 75.0,
                    "decision": "approve_paper_trade",
                    "route_status": "standard",
                    "hard_blocks": [],
                }
                trade_id = open_paper_trade(conn, lab_candidate, review, settings=settings)
                conn.execute(
                    """
                    insert into paper_trade_outcomes (
                        trade_id, horizon_minutes, measured_at, price, pnl_bps,
                        context_json, target_at, observed_at, delay_seconds,
                        measurement_status, price_source
                    ) values (?, 60, ?, ?, ?, '{}', ?, ?, 0, 'valid', 'test')
                    """,
                    (trade_id, created_at, 4.0, 14.0, created_at, created_at),
                )
            conn.commit()

            report = evaluate_strategy_lab(conn, settings)
            row = conn.execute(
                "select status, promoted_proposal_id from strategy_lab_experiments where strategy_lab_id = ?",
                ("okx_spot_survivor_lab_v1",),
            ).fetchone()
            rec = conn.execute("select action, payload_json from llm_recommendations").fetchone()

            self.assertEqual("promotion_queued", row["status"])
            self.assertIsNotNone(row["promoted_proposal_id"])
            self.assertEqual("propose_code_change", rec["action"])
            payload = json.loads(rec["payload_json"])
            self.assertEqual("strategy_lab_promotion", payload["change_category"])
            self.assertEqual("promotion_queued", report["evaluated"][0]["decision"])

    def test_summary_reports_recent_experiments(self):
        with memory_db() as conn:
            ingest_strategy_lab_recommendation(conn, lab_rec())
            summary = strategy_lab_summary(conn)
            self.assertEqual(1, summary["total_experiments"])
            self.assertEqual("okx_spot_survivor_lab_v1", summary["recent"][0]["strategy_lab_id"])


if __name__ == "__main__":
    unittest.main()

