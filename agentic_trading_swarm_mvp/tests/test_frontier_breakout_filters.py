import unittest

from src.frontier_breakout_filters import (
    BreakoutQualityConfig,
    breakout_confirmation_buffer_pct,
    is_high_conviction_breakout,
    score_breakout_quality,
)


class TestFrontierBreakoutFilters(unittest.TestCase):
    def test_confirmation_buffer_increases_above_atr_threshold(self):
        self.assertEqual(breakout_confirmation_buffer_pct(4.6), 0.35)
        self.assertEqual(breakout_confirmation_buffer_pct(4.5), 0.2)

    def test_high_conviction_requires_trend_liquidity_and_spread(self):
        self.assertTrue(
            is_high_conviction_breakout(
                price_above_vwap=True,
                price_above_20ema=True,
                rel_volume=1.8,
                spread_pct=0.35,
                atr_pct=3.0,
            )
        )
        self.assertFalse(
            is_high_conviction_breakout(
                price_above_vwap=False,
                price_above_20ema=True,
                rel_volume=2.0,
                spread_pct=0.2,
                atr_pct=3.0,
            )
        )

    def test_quality_score_penalizes_weak_confirmation(self):
        good = score_breakout_quality(price_above_vwap=True, price_above_20ema=True, rel_volume=2.0, spread_pct=0.2, atr_pct=3.0)
        weak = score_breakout_quality(price_above_vwap=False, price_above_20ema=False, rel_volume=1.0, spread_pct=0.5, atr_pct=5.0)
        self.assertGreater(good, weak)
        self.assertGreaterEqual(good, 80.0)
        self.assertLessEqual(weak, 50.0)
