from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import global_market_discovery_scanner as discovery_scanner
from strategy_reliability import apply_strategy_reliability
from yahoo_proxy_reuse import evaluate_yahoo_proxy_reuse


NOW = dt.datetime(2026, 8, 4, 14, 7, tzinfo=dt.timezone.utc)


def chart(
    *,
    timestamps: list[int] | None = None,
    closes: list[float] | None = None,
    opens: list[float | None] | None = None,
    market_state: str | None = None,
) -> dict:
    timestamps = timestamps or [
        int(dt.datetime(2026, 8, 4, 13, 15, tzinfo=dt.timezone.utc).timestamp()),
        int(dt.datetime(2026, 8, 4, 13, 30, tzinfo=dt.timezone.utc).timestamp()),
        int(dt.datetime(2026, 8, 4, 13, 45, tzinfo=dt.timezone.utc).timestamp()),
        int(dt.datetime(2026, 8, 4, 14, 0, tzinfo=dt.timezone.utc).timestamp()),
    ]
    closes = closes or [100.0, 100.1, 100.2, 100.3]
    opens = opens or [None, 100.05, 100.1, 100.2]
    meta = {
        "symbol": "TEST",
        "dataGranularity": "15m",
        "currentTradingPeriod": {
            "regular": {
                "start": int(dt.datetime(2026, 8, 4, 13, 30, tzinfo=dt.timezone.utc).timestamp()),
                "end": int(dt.datetime(2026, 8, 4, 20, 0, tzinfo=dt.timezone.utc).timestamp()),
            }
        },
    }
    if market_state is not None:
        meta["marketState"] = market_state
    return {
        "meta": meta,
        "timestamp": timestamps,
        "indicators": {
            "quote": [{"open": opens, "close": closes, "volume": [1_000_000] * len(closes)}]
        },
    }


class YahooProxyReuseGateTests(unittest.TestCase):
    def test_fresh_open_scheduled_bar_with_followthrough_is_reusable(self):
        result = evaluate_yahoo_proxy_reuse(chart(), now=NOW)

        self.assertTrue(result["proxy_valid_for_reuse"])
        self.assertEqual([], result["reasons"])
        self.assertEqual("open", result["source_session_status"])

    def test_closed_source_session_is_invalid_even_with_fresh_quote(self):
        result = evaluate_yahoo_proxy_reuse(
            chart(),
            now=dt.datetime(2026, 8, 4, 20, 1, tzinfo=dt.timezone.utc),
            settings={"yahoo_proxy_reuse_gate": {"max_quote_age_seconds": 30_000}},
        )

        self.assertFalse(result["proxy_valid_for_reuse"])
        self.assertIn("source_session_closed", result["reasons"])

    def test_quote_older_than_configured_bound_is_invalid(self):
        result = evaluate_yahoo_proxy_reuse(
            chart(),
            now=NOW,
            settings={"yahoo_proxy_reuse_gate": {"max_quote_age_seconds": 60}},
        )

        self.assertFalse(result["proxy_valid_for_reuse"])
        self.assertIn("proxy_quote_age_exceeded", result["reasons"])

    def test_latest_bar_behind_expected_schedule_is_invalid(self):
        lagged = chart()
        lagged["timestamp"] = lagged["timestamp"][:-1]
        lagged["indicators"]["quote"][0]["open"] = lagged["indicators"]["quote"][0]["open"][:-1]
        lagged["indicators"]["quote"][0]["close"] = lagged["indicators"]["quote"][0]["close"][:-1]
        lagged["indicators"]["quote"][0]["volume"] = lagged["indicators"]["quote"][0]["volume"][:-1]

        result = evaluate_yahoo_proxy_reuse(
            lagged,
            now=NOW,
            settings={"yahoo_proxy_reuse_gate": {"max_quote_age_seconds": 3600}},
        )

        self.assertFalse(result["proxy_valid_for_reuse"])
        self.assertIn("proxy_bar_schedule_lag_exceeded", result["reasons"])

    def test_opening_gap_without_directional_followthrough_is_invalid(self):
        result = evaluate_yahoo_proxy_reuse(
            chart(closes=[100.0, 102.0, 102.05, 102.1], opens=[None, 102.0, 102.0, 102.05]),
            now=NOW,
        )

        self.assertFalse(result["proxy_valid_for_reuse"])
        self.assertIn("opening_gap_without_live_followthrough", result["reasons"])
        self.assertLess(result["opening_gap_followthrough_ratio"], 0.25)

    def test_cross_surface_discovery_candidate_is_watch_only_when_proxy_invalid(self):
        current = int(dt.datetime.now(dt.timezone.utc).timestamp())
        invalid_chart = {
            "meta": {"symbol": "TEST", "marketState": "closed"},
            "timestamp": [current - (29 - index) * 900 for index in range(30)],
            "indicators": {
                "quote": [{"close": [100.0 + index * 0.1 for index in range(30)], "volume": [1_000_000] * 30}]
            },
        }
        original = discovery_scanner.fetch_chart
        try:
            discovery_scanner.fetch_chart = lambda _symbol: invalid_chart
            discovery_scanner._CHART_CACHE.clear()
            candidate = discovery_scanner._build_proxy_candidate(
                {"venue_or_source": "Another Market", "priority": 90, "confidence": 0.8},
                {"symbol": "TEST", "label": "test proxy", "surface": "equity_index_proxy"},
                {},
            )
        finally:
            discovery_scanner.fetch_chart = original
            discovery_scanner._CHART_CACHE.clear()

        self.assertFalse(candidate["proxy_valid_for_reuse"])
        self.assertEqual("watch_only", candidate["direction"])
        self.assertTrue(candidate["candidate_reject_reason"].startswith("proxy_invalid_for_reuse:"))

    def test_existing_candidate_loses_invalid_proxy_confirmation_score(self):
        candidate = {
            "venue": "OKX",
            "trade_type": "unrelated_paper_strategy",
            "direction": "long_frontier_perp",
            "score": 80.0,
            "score_before_proxy_confirmation": 70.0,
            "proxy_confirmation_score_boost": 10.0,
            "proxy_valid_for_reuse": False,
            "propagated_momentum_contribution": 0.8,
        }

        adjusted, _report = apply_strategy_reliability(
            [candidate], {"strategy_reliability": {"enabled": False}}
        )

        self.assertEqual(70.0, adjusted[0]["score"])
        self.assertEqual(0.0, adjusted[0]["effective_proxy_confirmation_weight"])
        self.assertEqual(0.0, adjusted[0]["propagated_momentum_contribution"])


if __name__ == "__main__":
    unittest.main()
