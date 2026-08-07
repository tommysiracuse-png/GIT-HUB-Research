from __future__ import annotations

import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import storage  # noqa: E402


class OpportunityExecutionLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = storage.connect(":memory:")
        self.candidate = {
            "seen_at": "2026-08-06T12:00:00+00:00",
            "venue": "TEST",
            "inst_id": "TEST:ABC-USD",
            "direction": "long",
            "trade_type": "lineage_test",
            "last": 100.0,
            "score": 80.0,
            "spread_bps": 2.0,
            "liquidity_score": 0.8,
        }
        self.review = {
            "decision": "pending_execution",
            "intended_decision": "approve_paper_trade",
            "learned_score": 80.0,
            "confidence": 0.8,
            "feasibility_status": "standard",
        }

    def tearDown(self) -> None:
        self.conn.close()

    def _opportunity(self, *, seen_at: str | None = None) -> int:
        candidate = dict(self.candidate)
        if seen_at is not None:
            candidate["seen_at"] = seen_at
        return storage.save_opportunity(self.conn, candidate, self.review)

    def test_pending_reconciliation_has_decision_index(self) -> None:
        indexes = {
            str(row["name"])
            for row in self.conn.execute("pragma index_list(opportunities)").fetchall()
        }

        self.assertIn("idx_opportunities_decision_id", indexes)

    def test_reconciler_recovers_filled_trade_after_interrupted_cycle(self) -> None:
        opportunity_id = self._opportunity()
        order = {
            "mode": "paper",
            "route_id": "paper_test",
            "status": "paper_filled",
            "notional_usd": 100.0,
        }
        order_id = storage.save_execution_order(
            self.conn,
            order,
            self.candidate,
            self.review,
            opportunity_id=opportunity_id,
        )
        trade_id = storage.open_paper_trade(
            self.conn,
            self.candidate,
            self.review,
            execution={
                "order_id": order_id,
                "order": order,
                "fills": [],
                "candidate": self.candidate,
                "opportunity_id": opportunity_id,
            },
        )

        result = storage.reconcile_pending_opportunities(self.conn)
        saved = self.conn.execute(
            "select decision,review_json from opportunities where id=?", (opportunity_id,)
        ).fetchone()
        saved_review = json.loads(saved["review_json"])

        self.assertEqual(1, result["reconciled"])
        self.assertEqual("paper_filled", saved["decision"])
        self.assertEqual(trade_id, saved_review["paper_trade_id"])
        self.assertEqual(order_id, saved_review["execution_order_id"])
        self.assertTrue(saved_review["reconciled_after_interruption"])

    def test_reconciler_marks_stale_unmaterialized_execution_abandoned(self) -> None:
        opportunity_id = self._opportunity(seen_at="2020-01-01T00:00:00+00:00")

        result = storage.reconcile_pending_opportunities(self.conn)
        saved = self.conn.execute(
            "select decision from opportunities where id=?", (opportunity_id,)
        ).fetchone()

        self.assertEqual({"execution_abandoned": 1}, result["by_decision"])
        self.assertEqual("execution_abandoned", saved["decision"])

    def test_maintenance_prunes_only_unreferenced_opportunities(self) -> None:
        linked_id = self._opportunity()
        unlinked_id = self._opportunity()
        storage.save_frontier_paper_shadow_observation(
            self.conn,
            {**self.candidate, "candidate_reject_reason": "test_shadow"},
            self.review,
            opportunity_id=linked_id,
        )

        report = storage.perform_maintenance(
            self.conn,
            {"maintenance": {"max_opportunity_rows": 1, "vacuum_after_prune": False}},
        )
        remaining = {
            int(row["id"])
            for row in self.conn.execute("select id from opportunities").fetchall()
        }

        self.assertEqual(1, report["opportunity_rows_deleted"])
        self.assertEqual({linked_id}, remaining)
        self.assertNotIn(unlinked_id, remaining)


if __name__ == "__main__":
    unittest.main()
