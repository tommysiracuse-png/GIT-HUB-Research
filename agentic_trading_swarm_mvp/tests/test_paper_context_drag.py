import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_context_drag import (  # noqa: E402
    apply_context_drag_overlay,
    context_drag_statistics,
    context_identifier,
    estimate_context_drag,
)
from paper_exploration import fair_lineage_order  # noqa: E402
from storage import connect  # noqa: E402


SETTINGS = {
    "mode": "paper",
    "allow_live_trading": False,
    "paper_exploration": {"enabled": True},
    "paper_context_drag": {"min_closed_trades": 2, "ranking_penalty_bps_cap": 30.0},
}


def candidate(score=80.0):
    return {
        "trade_type": "frontier_crypto_venue_map",
        "source_venue": "REFERENCE",
        "venue": "TARGET",
        "direction": "long_frontier_spot",
        "liquidity_score": 0.7,
        "latency_ms": 800,
        "gross_edge_bps_estimate": 20.0,
        "entry_slippage_bps_estimate": 3.0,
        "spread_bps": 8.0,
        "local_short_horizon_trend_bps": -5.0,
        "score": score,
        "paper_entry_blocked": False,
    }


class PaperContextDragTests(unittest.TestCase):
    def test_estimate_uses_all_requested_drag_components(self):
        detail = estimate_context_drag(candidate(), SETTINGS)

        self.assertEqual(
            detail["context_id"],
            "frontier_crypto_venue_map|REFERENCE|TARGET|long_frontier_spot|high|fast",
        )
        self.assertEqual(3.0, detail["components_bps"]["entry_slippage_proxy_bps"])
        self.assertEqual(4.0, detail["components_bps"]["spread_proxy_bps"])
        self.assertEqual(5.0, detail["components_bps"]["adverse_move_after_signal_bps"])
        self.assertGreater(detail["components_bps"]["delay_to_fill_decay_bps"], 0.0)
        self.assertAlmostEqual(20.0 - detail["estimated_drag_bps"], 7.984, places=3)

    def test_closed_context_is_down_ranked_but_never_blocked(self):
        weak = candidate()
        key = context_identifier(weak)
        stats = {key: {"closed_count": 2, "context_net_edge_bps": -18.0}}

        ranked = apply_context_drag_overlay([weak], stats, SETTINGS)

        self.assertEqual(1, len(ranked))
        self.assertFalse(weak["paper_entry_blocked"])
        self.assertEqual("ranked_not_blocked", weak["paper_context_drag_filter_status"])
        self.assertEqual("down_ranked_weak_context", weak["paper_context_drag"]["ranking_status"])
        self.assertLess(weak["paper_context_drag_ranking_score"], weak["score"])
        self.assertEqual("none", weak["paper_context_drag"]["eligibility_effect"])

    def test_overlay_changes_bounded_review_order_without_dropping_context(self):
        weak = candidate(score=90.0)
        weak["inst_id"] = "WEAK"
        strong = candidate(score=70.0)
        strong["inst_id"] = "STRONG"
        strong["source_venue"] = "OTHER_REFERENCE"
        stats = {
            context_identifier(weak): {"closed_count": 2, "context_net_edge_bps": -18.0},
        }

        apply_context_drag_overlay([weak, strong], stats, SETTINGS)
        ordered = fair_lineage_order([weak, strong], 0, SETTINGS)

        self.assertEqual(["STRONG", "WEAK"], [item["inst_id"] for item in ordered])
        self.assertEqual(2, len(ordered))
        self.assertFalse(weak["paper_entry_blocked"])

    def test_statistics_read_closed_paper_candidate_snapshots(self):
        conn = connect(":memory:")
        payload = candidate()
        drag = estimate_context_drag(payload, SETTINGS)
        payload["paper_context_drag"] = drag
        for pnl in (-12.0, -8.0):
            conn.execute(
                """
                insert into paper_trades (
                    opened_at, closed_at, venue, inst_id, direction, trade_type, signal_key,
                    base_score, learned_score, entry, exit, pnl_bps, status, thesis,
                    candidate_json, review_json
                ) values ('2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00',
                    'TARGET', 'ABC', 'long_frontier_spot', 'frontier_crypto_venue_map',
                    'frontier', 80, 80, 100, 99, ?, 'closed', '', ?, '{}')
                """,
                (pnl, json.dumps(payload)),
            )
        conn.commit()

        stats = context_drag_statistics(conn, SETTINGS)
        row = stats[drag["context_id"]]

        self.assertEqual(2, row["closed_count"])
        self.assertEqual(20.0, row["avg_raw_paper_alpha_bps"])
        self.assertEqual(round(20.0 - drag["estimated_drag_bps"], 3), row["context_net_edge_bps"])
        self.assertEqual(-10.0, row["avg_realized_pnl_bps"])


if __name__ == "__main__":
    unittest.main()
