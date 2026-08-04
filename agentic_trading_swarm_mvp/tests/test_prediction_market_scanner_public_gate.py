from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prediction_market_scanner as prediction
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    scanner = cfg.setdefault("prediction_market_scanner", {})
    scanner["orderbook_enrichment_top"] = 3
    scanner["polymarket_paper_gate_enabled"] = True
    scanner["polymarket_max_days_to_resolution"] = 30
    scanner["polymarket_min_liquidity_usd"] = 1_000
    scanner["polymarket_max_spread_bps"] = 300
    scanner["polymarket_require_visible_book"] = True
    return cfg


class PredictionMarketScannerPublicGateTests(unittest.TestCase):
    def test_polymarket_public_gate_keeps_only_fresh_liquid_markets_with_visible_quotes(self) -> None:
        old_fetch = prediction.fetch_json
        now = dt.datetime.now(dt.timezone.utc)
        fresh_end = (now + dt.timedelta(days=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        far_end = (now + dt.timedelta(days=60)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                return [
                    {
                        "id": "good",
                        "question": "Will BTC close above 80k this week?",
                        "slug": "btc-above-80k",
                        "outcomePrices": json.dumps([0.61, 0.39]),
                        "oneWeekPriceChange": "0.03",
                        "spread": "0.01",
                        "liquidityNum": "5000",
                        "volume24hr": "2000",
                        "endDate": fresh_end,
                        "clobTokenIds": json.dumps(["token-good-yes", "token-good-no"]),
                    },
                    {
                        "id": "far",
                        "question": "Will a distant event happen next quarter?",
                        "slug": "far-event",
                        "outcomePrices": json.dumps([0.58, 0.42]),
                        "oneWeekPriceChange": "0.02",
                        "spread": "0.01",
                        "liquidityNum": "6000",
                        "volume24hr": "1800",
                        "endDate": far_end,
                        "clobTokenIds": json.dumps(["token-far-yes", "token-far-no"]),
                    },
                    {
                        "id": "empty",
                        "question": "Will an ill-quoted event resolve soon?",
                        "slug": "empty-book",
                        "outcomePrices": json.dumps([0.56, 0.44]),
                        "oneWeekPriceChange": "0.02",
                        "spread": "0.01",
                        "liquidityNum": "7000",
                        "volume24hr": "2200",
                        "endDate": fresh_end,
                        "clobTokenIds": json.dumps(["token-empty-yes", "token-empty-no"]),
                    },
                ]
            if "token-good" in url:
                return {
                    "bids": [{"price": "0.60", "size": "100"}],
                    "asks": [{"price": "0.62", "size": "120"}],
                }
            if "token-far" in url:
                return {
                    "bids": [{"price": "0.57", "size": "100"}],
                    "asks": [{"price": "0.59", "size": "100"}],
                }
            if "token-empty" in url:
                return {"bids": [], "asks": []}
            raise AssertionError(url)

        prediction.fetch_json = fake_fetch
        try:
            rows, status = prediction._polymarket_candidates(settings(), limit=10)
        finally:
            prediction.fetch_json = old_fetch

        self.assertEqual([row["inst_id"] for row in rows], ["poly:good"])
        kept_source = rows[0]["data_source"]
        self.assertEqual(kept_source["paper_gate_status"], "pass")
        self.assertEqual(kept_source["paper_gate_reasons"], [])
        self.assertTrue(kept_source["outcome_timestamp_present"])
        self.assertTrue(kept_source["has_orderbook_token"])
        self.assertEqual(kept_source["orderbook_status"], "verified")
        self.assertEqual(status["paper_gate_filtered_count"], 2)
        self.assertEqual(status["paper_gate_reason_counts"]["too_far_from_resolution"], 1)
        self.assertEqual(status["paper_gate_reason_counts"]["missing_verified_orderbook"], 1)
        self.assertEqual(status["paper_gate_reason_counts"]["missing_visible_bid_ask"], 1)


if __name__ == "__main__":
    unittest.main()
