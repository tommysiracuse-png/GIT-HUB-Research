from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prediction_market_scanner as prediction
from settings import DEFAULT_SETTINGS


def settings() -> dict:
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    cfg.setdefault("prediction_market_scanner", {})["orderbook_enrichment_top"] = 2
    return cfg


class PredictionMarketScannerTests(unittest.TestCase):
    def test_build_scan_batch_writes_current_enriched_report(self) -> None:
        old_fetch = prediction.fetch_json
        old_runs = prediction.RUNS_DIR

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                return [
                    {
                        "id": "1",
                        "question": "Will Bitcoin hit a new high this week?",
                        "slug": "bitcoin-new-high",
                        "outcomePrices": json.dumps([0.55, 0.45]),
                        "oneWeekPriceChange": "0.02",
                        "spread": "0.01",
                        "liquidityNum": "5000",
                        "volume24hr": "1200",
                        "endDate": "2026-08-10T00:00:00Z",
                        "clobTokenIds": json.dumps(["token-yes", "token-no"]),
                    }
                ]
            if "clob.polymarket.com/book" in url:
                return {
                    "bids": [{"price": "0.54", "size": "100"}],
                    "asks": [{"price": "0.56", "size": "120"}],
                }
            if "external-api.kalshi.com/trade-api/v2/markets?" in url:
                return {
                    "markets": [
                        {
                            "ticker": "KXBTC",
                            "title": "Bitcoin above 70000 this week?",
                            "yes_bid": 45,
                            "yes_ask": 48,
                            "last_price": 47,
                            "volume_24h": 1000,
                            "open_interest": 5000,
                            "close_time": "2026-08-10T00:00:00Z",
                            "price_delta_24h": 2,
                        }
                    ]
                }
            if "orderbook" in url:
                return {"orderbook": {"yes": [[45, 100]], "no": [[52, 120]]}}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            prediction.fetch_json = fake_fetch
            prediction.RUNS_DIR = pathlib.Path(tmp)
            try:
                batch = prediction.build_scan_batch(settings(), limit=4)
                report = json.loads((pathlib.Path(tmp) / "prediction_markets_latest.json").read_text())
            finally:
                prediction.fetch_json = old_fetch
                prediction.RUNS_DIR = old_runs

        self.assertEqual(len(batch.candidates), 2)
        self.assertFalse(report["live_trading_allowed"])
        self.assertEqual(report["summary"]["candidate_count"], 2)
        self.assertIn("verified", report["summary"]["by_orderbook_status"])
        self.assertIn("crypto", report["summary"]["by_event_tag"])
        self.assertIn("prediction_markets_account", report["summary"]["route_blockers"])
        self.assertEqual(batch.candidates[0]["execution_feasibility"]["status"], "conditional")

    def test_kalshi_public_coverage_skips_expired_and_keeps_quote_fields(self) -> None:
        old_fetch = prediction.fetch_json

        def fake_fetch(url: str, timeout: int = 12):
            if "markets?" in url:
                return {
                    "markets": [
                        {
                            "ticker": "KXFRESH",
                            "title": "Fresh event market",
                            "category": "Economics",
                            "yes_bid": 45,
                            "yes_ask": 48,
                            "no_bid": 51,
                            "no_ask": 54,
                            "last_price": 47,
                            "volume_24h": 1000,
                            "open_interest": 5000,
                            "close_time": "2026-08-10T00:00:00Z",
                            "status": "active",
                        },
                        {
                            "ticker": "KXEXPIRED",
                            "title": "Expired event market",
                            "yes_bid": 45,
                            "yes_ask": 48,
                            "last_price": 47,
                            "close_time": "2020-01-01T00:00:00Z",
                        },
                    ]
                }
            if "orderbook" in url:
                return {"orderbook": {"yes": [[45, 100]], "no": [[52, 120]]}}
            raise AssertionError(url)

        prediction.fetch_json = fake_fetch
        try:
            rows = prediction.kalshi_candidates(settings(), limit=50)
        finally:
            prediction.fetch_json = old_fetch

        self.assertEqual([row["inst_id"] for row in rows], ["kalshi:KXFRESH"])
        source = rows[0]["data_source"]
        self.assertEqual(source["category"], "Economics")
        self.assertEqual(source["yes_bid"], 0.45)
        self.assertEqual(source["yes_ask"], 0.48)
        self.assertEqual(source["no_bid"], 0.51)
        self.assertEqual(source["no_ask"], 0.54)
        self.assertEqual(source["settlement_status"], "active")
        self.assertEqual(rows[0]["execution_feasibility"]["status"], "conditional")

    def test_polymarket_expired_markets_filtered_and_event_review_queue_reported(self) -> None:
        old_fetch = prediction.fetch_json
        old_runs = prediction.RUNS_DIR

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                return [
                    {
                        "id": "fresh",
                        "question": "Will obscure market resolve above 50?",
                        "slug": "obscure-market",
                        "outcomePrices": json.dumps([0.55, 0.45]),
                        "oneWeekPriceChange": "0.01",
                        "spread": "0.01",
                        "liquidityNum": "9000",
                        "volume24hr": "1200",
                        "endDate": "2026-08-10T00:00:00Z",
                        "clobTokenIds": json.dumps(["fresh-token"]),
                    },
                    {
                        "id": "expired",
                        "question": "Expired old market",
                        "outcomePrices": json.dumps([0.5, 0.5]),
                        "endDate": "2020-01-01T00:00:00Z",
                    },
                ]
            if "clob.polymarket.com/book" in url:
                return {
                    "bids": [{"price": "0.54", "size": "100"}],
                    "asks": [{"price": "0.56", "size": "100"}],
                }
            if "external-api.kalshi.com/trade-api/v2/markets?" in url:
                return {"markets": []}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            prediction.fetch_json = fake_fetch
            prediction.RUNS_DIR = pathlib.Path(tmp)
            try:
                batch = prediction.build_scan_batch(settings(), limit=5)
                report = json.loads((pathlib.Path(tmp) / "prediction_markets_latest.json").read_text())
            finally:
                prediction.fetch_json = old_fetch
                prediction.RUNS_DIR = old_runs

        summary = report["summary"]
        self.assertEqual([row["inst_id"] for row in batch.candidates], ["poly:fresh"])
        self.assertEqual(summary["expired_filtered_count"], 1)
        self.assertEqual(summary["by_resolution_risk_status"], {"normal": 1})
        self.assertEqual(summary["by_event_tag_confidence"], {"low": 1})
        self.assertTrue(summary["prediction_event_review_queue"])
        self.assertTrue(summary["prediction_market_research_queue"]["shadow_only"])
        self.assertEqual(summary["prediction_market_research_queue"]["group_counts"]["by_orderbook_status"], {"verified": 1})
        self.assertEqual(summary["by_liquidity_bucket"], {"moderate": 1})
        self.assertEqual(summary["event_review_shadow_trials"][0]["status"], "shadow_only")
        self.assertTrue(summary["provider_status"])


if __name__ == "__main__":
    unittest.main()
