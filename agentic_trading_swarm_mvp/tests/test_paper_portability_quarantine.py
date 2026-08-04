from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import strategy_reliability
import storage
from paper_order_router import apply_frontier_paper_guard, frontier_shadow_filter_reason
from strategy_reliability import (
    apply_strategy_reliability,
    evaluate_paper_cell_policy,
    paper_portability_quarantine_record,
)


def translated_candidate(**overrides: object) -> dict:
    candidate = {
        "source_market_family": "slow_macro_proxy",
        "destination_execution_family": "frontier_crypto",
        "venue": "BITGET",
        "inst_id": "BITGET:BTCUSDT",
        "asset_class": "crypto_derivatives",
        "market_surface": "perp",
        "trade_type": "portability_test_variant",
        "direction": "long_frontier_perp",
        "execution_mode": "paper",
        "score": 75.0,
        "edge_bps_estimate": 20.0,
        "gross_edge_bps_estimate": 30.0,
        "estimated_round_trip_cost_bps": 5.0,
        "quality_action": "normal",
        "anomaly_flags": [],
    }
    candidate.update(overrides)
    return candidate


class PaperPortabilityQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_json = strategy_reliability.REPORT_JSON
        self.old_md = strategy_reliability.REPORT_MD
        strategy_reliability.REPORT_JSON = pathlib.Path(self.tmp.name) / "reliability.json"
        strategy_reliability.REPORT_MD = pathlib.Path(self.tmp.name) / "reliability.md"

    def tearDown(self) -> None:
        strategy_reliability.REPORT_JSON = self.old_json
        strategy_reliability.REPORT_MD = self.old_md
        self.tmp.cleanup()

    def test_unproven_translation_is_clamped_to_neutral_and_shadowed(self) -> None:
        rows, report = apply_strategy_reliability([translated_candidate()])

        candidate = rows[0]
        self.assertEqual(0.0, candidate["score"])
        self.assertFalse(candidate["paper_rank_eligible"])
        self.assertFalse(candidate["promotion_eligible"])
        self.assertTrue(candidate["paper_entry_blocked"])
        self.assertEqual(
            "insufficient_destination_family_paper_evidence",
            candidate["paper_portability_quarantine"]["reason"],
        )
        self.assertEqual(1, report["summary"]["portability_quarantine_count"])

    def test_sufficient_non_positive_destination_expectancy_blocks_promotion(self) -> None:
        candidate = translated_candidate(closed_count=24, expectancy_net_bps=0.0)
        record = paper_portability_quarantine_record(candidate)
        policy = evaluate_paper_cell_policy(
            {
                **candidate,
                "avg_pnl_bps": 5.0,
                "win_rate": 0.60,
                "paper_route_status": "standard",
            },
            config={"paper_cell_policy": {"min_closed_trades": 3}},
        )

        self.assertEqual("non_positive_destination_family_expectancy", record["reason"])
        self.assertTrue(record["sufficient_closed_count"])
        self.assertTrue(record["promotion_blocked"])
        self.assertNotEqual("promoted", policy["decision"])
        self.assertIn(
            "non_positive_destination_family_expectancy",
            policy["promotion_gate"]["blockers"],
        )

    def test_positive_destination_proof_passes_portability_gate(self) -> None:
        candidate = translated_candidate(
            destination_family_paper_stats={
                "closed_count": 24,
                "expectancy_net_bps": 2.5,
            }
        )

        record = paper_portability_quarantine_record(candidate)
        rows, report = apply_strategy_reliability([candidate])

        self.assertTrue(record["eligible"])
        self.assertTrue(record["rank_above_neutral_allowed"])
        self.assertEqual("destination_family_proven", record["state"])
        self.assertEqual(75.0, rows[0]["score"])
        self.assertEqual(0, report["summary"]["portability_quarantine_count"])

    def test_runtime_hydrates_destination_proof_from_persisted_paper_stats(self) -> None:
        candidate = translated_candidate()
        candidate_key = storage.signal_key(candidate)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        try:
            conn.execute(
                """
                insert into signal_stats(
                    signal_key, closed_count, wins, avg_pnl_bps,
                    win_rate, score_adjustment, updated_at
                ) values (?, 24, 14, 3.5, 0.583, 1.0, '2026-08-04T00:00:00+00:00')
                """,
                (candidate_key,),
            )
            conn.commit()

            rows, _ = apply_strategy_reliability([candidate], conn=conn)
        finally:
            conn.close()

        proof = rows[0]["paper_portability_quarantine"]
        self.assertTrue(proof["eligible"])
        self.assertEqual(24, proof["closed_count"])
        self.assertEqual(3.5, proof["expectancy_net_bps"])
        self.assertEqual(
            "persisted_paper_signal_stats",
            rows[0]["destination_family_paper_stats"]["evidence_source"],
        )
        self.assertEqual(75.0, rows[0]["score"])

    def test_native_destination_family_and_live_context_are_out_of_scope(self) -> None:
        native = translated_candidate(
            source_market_family="crypto_spot",
            destination_execution_family="crypto_perp",
        )
        live = translated_candidate(execution_mode="live")

        self.assertIsNone(paper_portability_quarantine_record(native))
        self.assertIsNone(paper_portability_quarantine_record(live))

    def test_router_enforces_generic_cross_family_quarantine(self) -> None:
        candidate = translated_candidate(trade_type="frontier_crypto_venue_map")

        reason = frontier_shadow_filter_reason(candidate)
        guarded = apply_frontier_paper_guard(candidate)

        self.assertEqual("paper_cross_family_portability_quarantine", reason["guard"])
        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_fill_allowed"])
        self.assertFalse(guarded["promotion_eligible"])
        self.assertEqual(0.0, guarded["paper_allocation_multiplier"])


if __name__ == "__main__":
    unittest.main()
