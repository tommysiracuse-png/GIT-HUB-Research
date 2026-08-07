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

from agent_review import review_candidate  # noqa: E402
from execution_engine import execute_order  # noqa: E402
from route_resolver import enrich_candidates  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db  # noqa: E402
import strategy_reliability  # noqa: E402
from strategy_reliability import apply_strategy_reliability  # noqa: E402
from tests.test_proxy_route_activation import proxy_candidate  # noqa: E402


class PaperProxyScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)

    def _apply_reliability(self, candidate: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            old_json = strategy_reliability.REPORT_JSON
            old_md = strategy_reliability.REPORT_MD
            strategy_reliability.REPORT_JSON = pathlib.Path(tmp) / "strategy_reliability.json"
            strategy_reliability.REPORT_MD = pathlib.Path(tmp) / "strategy_reliability.md"
            try:
                rows, _ = apply_strategy_reliability([candidate], self.settings)
            finally:
                strategy_reliability.REPORT_JSON = old_json
                strategy_reliability.REPORT_MD = old_md
        return rows[0]

    def test_borrow_blocked_proxy_recovers_zero_direct_score_from_quality_evidence(self) -> None:
        candidate = proxy_candidate()
        candidate.update({"score": 0.0, "funding_bps": 0.0, "basis_bps": 0.0})

        routed = enrich_candidates([candidate], self.settings)[0]

        self.assertTrue(routed["paper_proxy_activated"])
        self.assertEqual(0.0, routed["pre_paper_proxy_score"])
        self.assertGreater(routed["score"], self.settings["scanner"]["min_base_score"])
        self.assertEqual(routed["score"], routed["proxy_quality_score"])
        self.assertIn(
            "direct_score_zero_replaced_by_proxy_quality_evidence",
            routed["proxy_quality_diagnostics"],
        )
        self.assertEqual(0.25, routed["paper_allocation_multiplier"])
        self.assertEqual("okx_derivatives_paper", routed["paper_proxy_counterfactual"]["route_id"])

    def test_weak_one_leg_proxy_is_scored_but_fails_closed_at_execution(self) -> None:
        candidate = proxy_candidate()
        candidate.update({"score": 0.0, "funding_bps": 0.0, "basis_bps": 0.0})
        routed = enrich_candidates([candidate], self.settings)[0]

        scored = self._apply_reliability(routed)
        review = review_candidate(scored, self.settings, {})

        self.assertGreater(scored["score"], 0.0)
        self.assertFalse(scored.get("paper_entry_blocked", False))
        self.assertEqual("reverse_basis_proxy_counterfactual", scored["strategy_reliability_action"])
        self.assertEqual(0.25, scored["strategy_reliability_allocation_multiplier"])
        self.assertEqual("approve_conditional_paper_trade", review["decision"])
        self.assertEqual([], review["hard_blocks"])
        self.assertTrue(any("paper proxy quality score" in item for item in review["evidence"]))
        self.assertTrue(any("counterfactual" in item for item in review["warnings"]))

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        execution = execute_order(conn, scored, review, self.settings)

        self.assertFalse(execution["paper_filled"])
        self.assertEqual([], execution["fills"])
        self.assertEqual(0.0, execution["order"]["notional_usd"])
        self.assertEqual(
            "blocked_paired_direct_requires_bounded_queue",
            execution["order"]["status"],
        )
        self.assertEqual(
            "invalid_or_incomplete",
            execution["order"]["paired_direct_contract_status"],
        )
        self.assertEqual("paper_proxy", execution["order"]["signal_stats_scope"])


if __name__ == "__main__":
    unittest.main()
