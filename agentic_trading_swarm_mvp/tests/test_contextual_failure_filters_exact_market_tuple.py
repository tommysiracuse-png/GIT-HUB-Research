import pathlib
import sqlite3
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from contextual_failure_filters import _build_groups, _upsert_contextual_stats, build_context_features


class ContextualFailureExactMarketTupleTests(unittest.TestCase):
    def setUp(self):
        self.signal_key = "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional"
        self.candidate = {
            "venue": "OKX_SPOT",
            "inst_id": "BTC-USDT",
            "direction": "conditional",
            "trade_type": "short_frontier_spot",
            "base": "BTC",
            "quote": "USDT",
            "spread_bps": 4.2,
            "liquidity_score": 0.82,
            "edge_bps_estimate": 61.388,
            "data_status": "fresh",
            "seen_at": "2026-07-30T07:23:29+00:00",
        }
        self.review = {
            "net_edge_bps_estimate": 61.388,
            "route_status": "paper_ready",
        }

    def test_build_context_features_exposes_exact_market_tuple_key(self):
        features = build_context_features(
            self.candidate,
            self.review,
            fallback_time="2026-07-30T07:23:29+00:00",
        )
        self.assertEqual(features["trade_type"], "short_frontier_spot")
        self.assertEqual(features["direction"], "conditional")
        self.assertEqual(features["market_context_key"], "OKX_SPOT|short_frontier_spot|conditional")

    def test_report_groups_include_exact_market_tuple_without_policy_filter(self):
        features = build_context_features(
            self.candidate,
            self.review,
            fallback_time="2026-07-30T07:23:29+00:00",
        )
        trades = [
            {
                "signal_key": self.signal_key,
                "pnl_bps": 61.388,
                "candidate": self.candidate,
                "review": self.review,
                "features": features,
            }
        ]
        groups = _build_groups(trades, settings={})
        by_dimension = {(item["dimension"], item["value"]): item for item in groups}

        self.assertIn(("trade_type", "short_frontier_spot"), by_dimension)
        self.assertIn(("direction", "conditional"), by_dimension)
        self.assertIn(("market_context_key", "OKX_SPOT|short_frontier_spot|conditional"), by_dimension)
        self.assertEqual(by_dimension[("trade_type", "short_frontier_spot")]["context_filter"], {})
        self.assertEqual(by_dimension[("direction", "conditional")]["context_filter"], {})
        self.assertEqual(
            by_dimension[("market_context_key", "OKX_SPOT|short_frontier_spot|conditional")]["context_filter"],
            {},
        )

    def test_exact_market_tuple_rows_do_not_upsert_contextual_policy_stats(self):
        features = build_context_features(
            self.candidate,
            self.review,
            fallback_time="2026-07-30T07:23:29+00:00",
        )
        groups = _build_groups(
            [{"signal_key": self.signal_key, "pnl_bps": 61.388, "candidate": self.candidate, "review": self.review, "features": features}],
            settings={},
        )
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            create table contextual_stats (
                context_key text primary key,
                closed_count integer,
                wins integer,
                avg_pnl_bps real,
                win_rate real,
                updated_at text
            )
            """
        )
        _upsert_contextual_stats(conn, groups)
        keys = [row[0] for row in conn.execute("select context_key from contextual_stats order by context_key").fetchall()]
        self.assertTrue(any(key.endswith("|signal_family=all") for key in keys))
        self.assertFalse(any("|trade_type=" in key or "|direction=" in key or "|market_context_key=" in key for key in keys))
