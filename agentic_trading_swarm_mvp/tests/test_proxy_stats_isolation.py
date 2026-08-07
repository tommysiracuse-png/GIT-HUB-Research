from __future__ import annotations

import copy
import pathlib
import sqlite3
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import learning  # noqa: E402
from agent_review import review_candidate  # noqa: E402
from route_resolver import enrich_candidates  # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402
from storage import init_db, open_paper_trade, performance_summary, signal_key  # noqa: E402
from tests.test_proxy_route_activation import proxy_candidate  # noqa: E402


def review_for(candidate: dict) -> dict:
    return {
        "decision": "approve_paper_trade",
        "learned_score": candidate["score"],
        "feasibility_status": (candidate.get("execution_feasibility") or {}).get("status"),
        "route_id": candidate.get("route_id"),
        "route_status": candidate.get("route_status"),
    }


class ProxyStatsIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_proxy_key_does_not_match_direct_route_policy(self) -> None:
        candidate = enrich_candidates([proxy_candidate()], self.settings)[0]
        direct_key = candidate["direct_signal_key"]
        policy = {
            "policy_id": "direct-route-governor",
            "signal_key": direct_key,
            "policy_type": "safety_governor",
            "pause_entries": True,
            "allocation_multiplier": 0.0,
            "min_score_delta": 0.0,
            "policy": {},
        }

        review = review_candidate(candidate, self.settings, {}, policies=[policy])

        self.assertNotEqual(direct_key, review["signal_key"])
        self.assertTrue(review["signal_key"].startswith("PAPER_PROXY|"))
        self.assertEqual([], review["applied_policies"])

    def test_proxy_open_trade_is_excluded_from_direct_headline_count(self) -> None:
        proxy = enrich_candidates([proxy_candidate()], self.settings)[0]
        open_paper_trade(
            self.conn,
            proxy,
            review_for(proxy),
            settings=self.settings,
        )

        summary = performance_summary(self.conn)

        self.assertEqual(0, summary["open"])

    def test_legacy_one_leg_pair_outcomes_never_enter_learning_stats(self) -> None:
        proxy = enrich_candidates([proxy_candidate()], self.settings)[0]
        direct = proxy_candidate()
        direct["execution_feasibility"] = {"status": "conditional"}
        direct.pop("signal_key", None)

        self.assertNotEqual(signal_key(direct), signal_key(proxy))
        proxy_trade_id = open_paper_trade(
            self.conn,
            proxy,
            review_for(proxy),
            settings=self.settings,
        )
        direct_trade_id = open_paper_trade(
            self.conn,
            direct,
            review_for(direct),
            settings=self.settings,
        )
        self.conn.execute(
            "update paper_trades set status = 'closed', pnl_bps = 12.0, closed_at = datetime('now'), close_measurement_status = 'valid' where id = ?",
            (proxy_trade_id,),
        )
        self.conn.execute(
            "update paper_trades set status = 'closed', pnl_bps = -8.0, closed_at = datetime('now'), close_measurement_status = 'valid' where id = ?",
            (direct_trade_id,),
        )
        self.conn.commit()

        with patch.object(learning, "generate_improvement_tasks"), patch.object(
            learning, "generate_growth_experiments"
        ), patch.object(learning, "write_backlog"), patch.object(learning, "write_growth_plan"):
            stats = learning.update_signal_stats(self.conn, self.settings)

        self.assertNotIn(signal_key(proxy), stats)
        self.assertNotIn(signal_key(direct), stats)
        proxy_context = self.conn.execute(
            "select closed_count, avg_pnl_bps from contextual_stats where context_key = ?",
            ("paper_proxy|direction:long_perp_short_spot",),
        ).fetchone()
        direct_context = self.conn.execute(
            "select closed_count, avg_pnl_bps from contextual_stats where context_key = ?",
            ("direction:long_perp_short_spot",),
        ).fetchone()
        self.assertIsNone(proxy_context)
        self.assertIsNone(direct_context)


if __name__ == "__main__":
    unittest.main()
