from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from execution_engine import execute_order
from paper_order_router import apply_frontier_paper_guard
from paper_decay_quarantine import quarantine_record, runtime_report
from paper_exploration_report import build_paper_exploration_report
from settings import DEFAULT_SETTINGS
from storage import connect
from strategy_reliability import apply_strategy_reliability


class OkxBasisDecayQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings["mode"] = "paper"
        self.settings["allow_live_trading"] = False

    @staticmethod
    def candidate(**overrides: object) -> dict:
        candidate = {
            "venue": "OKX",
            "inst_id": "BTC-USDT-SWAP",
            "trade_type": "perp_funding_basis",
            "direction": "basis_mean_reversion_short_perp",
            "last": 100.0,
            "score": 80.0,
            "execution_feasibility": {"status": "standard", "route_status": "standard"},
            "execution_route": {"route_status": "standard"},
            "thesis": "priceable basis observation",
        }
        candidate.update(overrides)
        return candidate

    def test_only_named_okx_families_become_observe_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            decayed = self.candidate()
            funding_capture = self.candidate(direction="funding_capture_long_perp")
            healthy_carry = self.candidate(direction="short_perp_long_spot")

            rows, report = apply_strategy_reliability(
                [decayed, funding_capture, healthy_carry], self.settings, conn=conn
            )

            by_direction = {row["direction"]: row for row in rows}
            blocked = by_direction["basis_mean_reversion_short_perp"]
            self.assertEqual("decay_quarantine", blocked["candidate_reject_reason"])
            self.assertEqual("shadow_trial", blocked["paper_action"])
            self.assertEqual("observe_only", blocked["paper_execution_mode"])
            self.assertFalse(blocked["paper_fill_allowed"])
            self.assertNotIn("paper_okx_basis_decay_quarantine", by_direction["funding_capture_long_perp"])
            self.assertNotIn("paper_okx_basis_decay_quarantine", by_direction["short_perp_long_spot"])
            self.assertEqual(1, report["summary"]["okx_basis_decay_quarantine_count"])
            conn.close()

    def test_conditional_reverse_basis_is_quarantined_but_standard_is_not(self) -> None:
        conditional = self.candidate(
            direction="long_perp_short_spot",
            execution_feasibility={"status": "conditional", "route_status": "conditional"},
        )
        standard = self.candidate(direction="long_perp_short_spot")

        self.assertTrue(quarantine_record(conditional, self.settings)["active"])
        self.assertIsNone(quarantine_record(standard, self.settings))

    def test_execution_never_emits_a_paper_fill_for_quarantined_family(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            review = {"paper_allocation_multiplier": 1.0, "decision": "approve_paper_trade"}

            execution = execute_order(conn, self.candidate(), review, self.settings)

            self.assertFalse(execution["paper_filled"])
            self.assertEqual("shadow_filtered", execution["order"]["status"])
            self.assertEqual("shadow_trial", execution["candidate"]["paper_action"])
            self.assertEqual("decay_quarantine", execution["candidate"]["candidate_reject_reason"])
            conn.close()

    def test_router_marks_target_as_shadow_trial_observe_only(self) -> None:
        guarded = apply_frontier_paper_guard(
            self.candidate(paper_filled=True, status="paper_filled"), self.settings
        )

        self.assertTrue(guarded["shadow_filtered"])
        self.assertFalse(guarded["paper_filled"])
        self.assertEqual("shadow_trial", guarded["paper_action"])
        self.assertEqual("observe_only", guarded["paper_execution_mode"])

    def test_runtime_report_releases_after_one_hundred_closed_target_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            candidate = self.candidate()
            first = quarantine_record(candidate, self.settings, conn=conn)
            self.assertTrue(first["active"])
            closed_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
            for index in range(100):
                conn.execute(
                    """
                    insert into paper_trades (
                        opened_at, closed_at, venue, inst_id, direction, trade_type,
                        signal_key, base_score, learned_score, entry, exit, pnl_bps,
                        status, thesis, candidate_json, review_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)
                    """,
                    (
                        closed_at,
                        closed_at,
                        "OKX",
                        f"BTC-{index}-USDT-SWAP",
                        candidate["direction"],
                        candidate["trade_type"],
                        "OKX|perp_funding_basis|basis_mean_reversion_short_perp|standard",
                        80.0,
                        80.0,
                        100.0,
                        101.0,
                        10.0,
                        "shadow label",
                        json.dumps(candidate),
                        "{}",
                    ),
                )
            conn.commit()

            report = runtime_report(conn, self.settings)
            paper_report = build_paper_exploration_report(conn, self.settings)

            self.assertEqual("released", report["status"])
            self.assertEqual("closed_label_limit_reached", report["release_reason"])
            self.assertEqual(100, report["closed_label_count"])
            self.assertEqual(10.0, report["avg_pnl_bps"])
            self.assertEqual(1.0, report["win_rate"])
            self.assertEqual("decay_quarantine", paper_report["okx_basis_decay_quarantine"]["reason"])
            self.assertEqual(100, paper_report["summary"]["okx_basis_decay_quarantine_closed_labels"])
            conn.close()

    def test_runtime_report_releases_after_the_fourteen_day_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(pathlib.Path(temp_dir) / "radar.sqlite")
            quarantine_record(self.candidate(), self.settings, conn=conn)
            conn.execute(
                "update paper_decay_quarantines set expires_at = ?",
                ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),),
            )
            conn.commit()

            report = runtime_report(conn, self.settings)

            self.assertEqual("released", report["status"])
            self.assertEqual("duration_elapsed", report["release_reason"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
