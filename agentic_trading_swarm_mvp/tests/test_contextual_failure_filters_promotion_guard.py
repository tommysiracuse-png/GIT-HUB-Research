import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from contextual_failure_filters import _build_groups


class StrategyLabPromotionGuardTests(unittest.TestCase):
    def _trade(self, signal_key: str, market_context_key: str, pnl_bps: float) -> dict:
        venue, trade_type, direction = market_context_key.split("|", 2)
        return {
            "signal_key": signal_key,
            "pnl_bps": pnl_bps,
            "features": {
                "venue": venue,
                "trade_type": trade_type,
                "direction": direction,
                "market_context_key": market_context_key,
            },
        }

    def _find_group(self, groups: list[dict], signal_key: str, dimension: str, value: str) -> dict:
        for item in groups:
            if item["signal_key"] == signal_key and item["dimension"] == dimension and item["value"] == value:
                return item
        raise AssertionError(f"missing group for {signal_key} {dimension}={value}")

    def test_strategy_lab_signal_family_reports_exact_segment_promotion_scope(self):
        signal_key = "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional"
        market_context_key = "OKX_SPOT|short_frontier_spot|conditional"
        settings = {
            "contextual_failure_filters": {
                "strategy_lab_exact_market_promotion_guard": True,
                "min_closed_for_filter": 2,
                "promotion_min_closed": 2,
                "promotion_min_win_rate": 0.5,
                "promotion_min_avg_pnl_bps": 0.0,
            }
        }

        groups = _build_groups(
            [
                self._trade(signal_key, market_context_key, 45.0),
                self._trade(signal_key, market_context_key, 15.0),
            ],
            settings,
        )

        signal_family = self._find_group(groups, signal_key, "signal_family", "all")
        exact_market = self._find_group(groups, signal_key, "market_context_key", market_context_key)

        self.assertEqual(signal_family["promotion_scope"], "segment_matched_only")
        self.assertEqual(signal_family["promotion_guard"], "segment_matched_only")
        self.assertEqual(signal_family["promotable_market_context_keys"], [market_context_key])

        self.assertEqual(exact_market["promotion_scope"], "exact_market_context_only")
        self.assertEqual(exact_market["promotion_guard"], "eligible_exact_market_context")
        self.assertEqual(exact_market["promotable_market_context_keys"], [market_context_key])

    def test_strategy_lab_signal_family_blocks_promotion_without_exact_segment_evidence(self):
        signal_key = "STRATEGY_LAB|route_rich_frontier_long_filter_2942c975|OKX_SPOT|short_frontier_spot|conditional"
        market_context_key = "OKX_SPOT|short_frontier_spot|conditional"
        settings = {
            "contextual_failure_filters": {
                "strategy_lab_exact_market_promotion_guard": True,
                "min_closed_for_filter": 3,
                "promotion_min_closed": 3,
                "promotion_min_win_rate": 0.5,
                "promotion_min_avg_pnl_bps": 0.0,
            }
        }

        groups = _build_groups(
            [
                self._trade(signal_key, market_context_key, 40.0),
                self._trade(signal_key, market_context_key, 10.0),
            ],
            settings,
        )

        signal_family = self._find_group(groups, signal_key, "signal_family", "all")
        exact_market = self._find_group(groups, signal_key, "market_context_key", market_context_key)

        self.assertEqual(signal_family["promotion_scope"], "segment_matched_only")
        self.assertEqual(signal_family["promotion_guard"], "blocked_pending_segment_evidence")
        self.assertEqual(signal_family["promotable_market_context_keys"], [])

        self.assertEqual(exact_market["promotion_scope"], "exact_market_context_only")
        self.assertEqual(exact_market["promotion_guard"], "blocked_pending_segment_evidence")
        self.assertEqual(exact_market["promotable_market_context_keys"], [])

    def test_non_strategy_lab_signals_remain_unannotated(self):
        signal_key = "frontier_dislocation_existing"
        market_context_key = "OKX_SPOT|short_frontier_spot|conditional"
        settings = {"contextual_failure_filters": {"strategy_lab_exact_market_promotion_guard": True}}

        groups = _build_groups(
            [
                self._trade(signal_key, market_context_key, 25.0),
                self._trade(signal_key, market_context_key, 15.0),
            ],
            settings,
        )

        signal_family = self._find_group(groups, signal_key, "signal_family", "all")
        exact_market = self._find_group(groups, signal_key, "market_context_key", market_context_key)

        self.assertNotIn("promotion_scope", signal_family)
        self.assertNotIn("promotion_guard", signal_family)
        self.assertNotIn("promotion_scope", exact_market)
        self.assertNotIn("promotion_guard", exact_market)
