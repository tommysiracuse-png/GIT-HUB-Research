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
    def test_prediction_adapter_remains_public_data_only_when_capability_is_enabled(self) -> None:
        cfg = settings()
        cfg["account_capabilities"]["prediction_markets"] = True
        route = prediction.feasibility(cfg)

        self.assertEqual(route["status"], "conditional")
        self.assertTrue(route["paper_only"])
        self.assertTrue(route["public_data_only"])
        self.assertFalse(route["live_execution_supported"])
        self.assertTrue(route["execution_disabled"])
        self.assertTrue(route["order_routing_disabled"])
        self.assertEqual(route["route_id"], prediction.POLYMARKET_PAPER_ROUTE)

    def test_prediction_report_cannot_advertise_live_trading(self) -> None:
        old_runs = prediction.RUNS_DIR
        cfg = settings()
        cfg["mode"] = "live"
        cfg["allow_live_trading"] = True
        with tempfile.TemporaryDirectory() as tmp:
            prediction.RUNS_DIR = pathlib.Path(tmp)
            try:
                path = prediction.write_outputs([], settings=cfg)
                report = json.loads(path.read_text())
            finally:
                prediction.RUNS_DIR = old_runs

        self.assertEqual(report["mode"], "paper")
        self.assertFalse(report["live_trading_allowed"])

    def test_build_scan_batch_writes_current_enriched_report(self) -> None:
        old_fetch = prediction.fetch_json
        old_runs = prediction.RUNS_DIR
        book_timestamp = str(
            int(prediction.dt.datetime.now(prediction.dt.timezone.utc).timestamp() * 1000)
        )

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
                    "timestamp": book_timestamp,
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
        self.assertEqual(
            report["summary"]["by_signal_surface"]["prediction_market_probability"],
            2,
        )
        self.assertEqual(report["summary"]["paper_measurement"]["fresh_candidate_count"], 1)
        self.assertEqual(report["summary"]["paper_measurement"]["order_routing_disabled_count"], 2)
        kalshi_metrics = report["summary"]["paper_measurement"]["kalshi_probability_measurement"]
        self.assertEqual("neutral_shrinkage_calibration_v1", kalshi_metrics["model_name"])
        self.assertEqual(1, kalshi_metrics["candidate_count"])
        self.assertEqual("pending until resolved outcomes are observed", kalshi_metrics["calibration_error"])
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

    def test_kalshi_current_dollar_schema_and_reject_accounting(self) -> None:
        old_fetch = prediction.fetch_json
        old_runs = prediction.RUNS_DIR

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                return []
            if "markets?" in url:
                return {
                    "markets": [
                        {
                            "ticker": "KXDOLLARS",
                            "title": "Will the policy rate fall this year?",
                            "yes_bid_dollars": "0.42",
                            "yes_ask_dollars": "0.45",
                            "no_bid_dollars": "0.55",
                            "no_ask_dollars": "0.58",
                            "last_price_dollars": "0.44",
                            "liquidity_dollars": "2500.50",
                            "volume_24h_fp": "825.25",
                            "open_interest_fp": "1400.75",
                            "close_time": "2026-08-10T00:00:00Z",
                            "status": "active",
                        },
                        {
                            "ticker": "KXNOPRICE",
                            "title": "Unquoted multivariate market",
                            "last_price_dollars": "0.00",
                            "liquidity_dollars": "0.00",
                            "close_time": "2026-08-10T00:00:00Z",
                            "status": "active",
                        },
                        {
                            "ticker": "KXNODATE",
                            "title": "Missing date market",
                            "last_price_dollars": "0.52",
                            "volume_24h_fp": "100",
                            "status": "active",
                        },
                    ]
                }
            if "orderbook" in url:
                return {"orderbook_fp": {"yes_dollars": [["0.42", "100"]], "no_dollars": [["0.55", "80"]]}}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            prediction.fetch_json = fake_fetch
            prediction.RUNS_DIR = pathlib.Path(tmp)
            try:
                batch = prediction.build_scan_batch(settings(), limit=10)
                report = json.loads((pathlib.Path(tmp) / "prediction_markets_latest.json").read_text())
            finally:
                prediction.fetch_json = old_fetch
                prediction.RUNS_DIR = old_runs

        self.assertEqual([row["inst_id"] for row in batch.candidates], ["kalshi:KXDOLLARS"])
        source = batch.candidates[0]["data_source"]
        self.assertEqual(source["yes_bid"], 0.42)
        self.assertEqual(source["yes_ask"], 0.45)
        self.assertEqual(source["kalshi_price_schema"], "dollars_fixed_point")
        self.assertEqual(source["orderbook_status"], "verified")
        self.assertEqual(
            report["summary"]["provider_reject_reason_counts"],
            {"missing_price": 1, "missing_end_date": 1},
        )
        kalshi_status = next(item for item in report["summary"]["provider_status"] if item["provider"] == "KALSHI")
        self.assertEqual(kalshi_status["multivariate_filter"], "exclude")
        self.assertEqual(kalshi_status["fetched_pages"], 1)

    def test_kalshi_uses_fair_value_direction_and_keeps_quality_gates_diagnostic(self) -> None:
        old_fetch = prediction.fetch_json
        now = prediction.dt.datetime.now(prediction.dt.timezone.utc)
        near = (now + prediction.dt.timedelta(minutes=30)).isoformat()
        far = (now + prediction.dt.timedelta(days=45)).isoformat()

        def fake_fetch(url: str, timeout: int = 12):
            if "markets?" in url:
                return {
                    "markets": [
                        {
                            "ticker": "KXHIGHYES",
                            "title": "Will this high-probability event happen?",
                            "yes_bid_dollars": "0.79",
                            "yes_ask_dollars": "0.81",
                            "last_price_dollars": "0.80",
                            "close_time": near,
                            "status": "open",
                        },
                        {
                            "ticker": "KXFARLOWLIQ",
                            "title": "Will this distant low-liquidity event happen?",
                            "last_price_dollars": "0.40",
                            "close_time": far,
                            "status": "open",
                        },
                    ]
                }
            if "orderbook" in url:
                return {"orderbook_fp": {"yes_dollars": [["0.79", "50"]], "no_dollars": [["0.19", "50"]]}}
            raise AssertionError(url)

        prediction.fetch_json = fake_fetch
        try:
            rows = prediction.kalshi_candidates(settings(), limit=10)
        finally:
            prediction.fetch_json = old_fetch

        self.assertEqual([row["inst_id"] for row in rows], ["kalshi:KXHIGHYES", "kalshi:KXFARLOWLIQ"])
        high = rows[0]
        self.assertEqual("buy_no_event", high["direction"])
        self.assertEqual(0.8, high["yes_probability"])
        self.assertEqual(0.71, high["fair_probability"])
        self.assertEqual(0.2, high["last"])
        self.assertEqual("diagnostic_only", high["data_source"]["paper_gate_status"])
        self.assertIn("too_near_resolution", high["data_source"]["paper_gate_reasons"])

        low_liquidity = rows[1]["data_source"]
        self.assertEqual("diagnostic_only", low_liquidity["paper_gate_status"])
        self.assertIn("outside_short_event_window", low_liquidity["paper_gate_reasons"])
        self.assertIn("below_minimum_liquidity_diagnostic", low_liquidity["paper_gate_reasons"])
        self.assertEqual("pending_resolution", low_liquidity["realized_brier_status"])

    def test_event_tags_use_token_boundaries_and_world_cup_phrases(self) -> None:
        netherlands = prediction._event_tag_details(
            {"title": "Will the Netherlands win the FIFA World Cup tournament?"}
        )
        ethereum = prediction._event_tag_details({"title": "Will ETH outperform Bitcoin?"})

        self.assertIn("sports", netherlands["tags"])
        self.assertNotIn("crypto", netherlands["tags"])
        self.assertIn("crypto", ethereum["tags"])

    def test_polymarket_expired_markets_filtered_and_event_review_queue_reported(self) -> None:
        old_fetch = prediction.fetch_json
        old_runs = prediction.RUNS_DIR
        book_timestamp = str(
            int(prediction.dt.datetime.now(prediction.dt.timezone.utc).timestamp() * 1000)
        )

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
                        "clobTokenIds": json.dumps(["fresh-yes-token", "fresh-no-token"]),
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
                    "timestamp": book_timestamp,
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

    def test_polymarket_normalizes_binary_yes_no_books_and_freshness(self) -> None:
        old_fetch = prediction.fetch_json
        now = prediction.dt.datetime.now(prediction.dt.timezone.utc)
        book_time = (now - prediction.dt.timedelta(minutes=4)).replace(microsecond=0)
        expiry = (now + prediction.dt.timedelta(days=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                self.assertIn("order=liquidity", url)
                self.assertIn("ascending=false", url)
                return [
                    {
                        "id": "binary-1",
                        "question": "Will the policy rate be cut this month?",
                        "outcomes": json.dumps(["Yes", "No"]),
                        "outcomePrices": json.dumps(["0.42", "0.58"]),
                        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
                        "endDate": expiry,
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                        "liquidityNum": "25000",
                        "volume24hr": "4000",
                    }
                ]
            if "yes-token" in url:
                return {
                    "timestamp": str(int(book_time.timestamp() * 1000)),
                    "neg_risk": False,
                    "bids": [{"price": "0.41", "size": "100"}],
                    "asks": [{"price": "0.43", "size": "120"}],
                }
            if "no-token" in url:
                return {
                    "timestamp": str(int(book_time.timestamp() * 1000)),
                    "neg_risk": False,
                    "bids": [{"price": "0.57", "size": "90"}],
                    "asks": [{"price": "0.59", "size": "110"}],
                }
            raise AssertionError(url)

        prediction.fetch_json = fake_fetch
        try:
            rows, status = prediction._polymarket_candidates(settings(), limit=150)
        finally:
            prediction.fetch_json = old_fetch

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(status["market_cap"], 100)
        self.assertEqual(status["requested_limit"], 100)
        self.assertEqual(row["venue"], "POLYMARKET")
        self.assertEqual(row["market_id"], "binary-1")
        self.assertEqual(row["title"], "Will the policy rate be cut this month?")
        self.assertEqual(row["probability_mid"], 0.42)
        self.assertEqual(row["yes_probability"], 0.42)
        self.assertEqual(row["no_probability"], 0.58)
        self.assertEqual(row["best_bid"], 0.41)
        self.assertEqual(row["best_ask"], 0.43)
        self.assertEqual(row["yes_best_bid"], 0.41)
        self.assertEqual(row["yes_best_ask"], 0.43)
        self.assertEqual(row["no_best_bid"], 0.57)
        self.assertEqual(row["no_best_ask"], 0.59)
        self.assertEqual(row["spread_bps"], 200.0)
        self.assertGreater(row["depth_usd"], 0.0)
        self.assertGreaterEqual(row["stale_minutes"], 3.9)
        self.assertLess(row["stale_minutes"], 5.0)
        self.assertEqual(row["expiry"], expiry)
        self.assertTrue(row["paper_only"])
        self.assertTrue(row["read_only"])
        self.assertEqual(row["freshness_status"], "fresh")
        self.assertTrue(row["execution_disabled"])
        self.assertTrue(row["order_routing_disabled"])

    def test_polymarket_excludes_resolved_and_ambiguous_markets(self) -> None:
        old_fetch = prediction.fetch_json
        expiry = (prediction.dt.datetime.now(prediction.dt.timezone.utc) + prediction.dt.timedelta(days=5)).isoformat()

        def fake_fetch(url: str, timeout: int = 12):
            if "gamma-api.polymarket.com/markets?" in url:
                base = {
                    "question": "Example?",
                    "outcomePrices": json.dumps([0.5, 0.5]),
                    "clobTokenIds": json.dumps(["yes", "no"]),
                    "endDate": expiry,
                    "liquidityNum": "1000",
                }
                return [
                    {**base, "id": "resolved", "resolved": True},
                    {**base, "id": "neg-risk", "negRisk": True},
                    {**base, "id": "multi", "outcomes": json.dumps(["A", "B", "C"])},
                    {**base, "id": "one-token", "clobTokenIds": json.dumps(["yes"])},
                ]
            raise AssertionError(url)

        prediction.fetch_json = fake_fetch
        try:
            rows, status = prediction._polymarket_candidates(settings(), limit=100)
        finally:
            prediction.fetch_json = old_fetch

        self.assertEqual(rows, [])
        self.assertEqual(status["rejected_count"], 4)
        self.assertEqual(status["reject_reason_counts"]["closed_or_resolved"], 1)
        self.assertEqual(status["reject_reason_counts"]["ambiguous_multi_condition"], 1)
        self.assertEqual(status["reject_reason_counts"]["ambiguous_outcomes"], 1)
        self.assertEqual(status["reject_reason_counts"]["missing_binary_token_pair"], 1)


if __name__ == "__main__":
    unittest.main()
