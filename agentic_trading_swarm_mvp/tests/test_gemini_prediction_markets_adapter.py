from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.gemini_prediction_markets import (
    BTCUSD_TICKER_URL,
    DOCS_URL,
    GeminiBtcFiveMinutePredictionMarketsAdapter,
    events_url,
    parse_gemini_btc_5m_events,
    parse_gemini_btcusd_ticker,
)


ACTIVE_EVENTS = {
    "data": [
        {
            "id": "179440",
            "title": "BTC price today at 11:30pm EDT",
            "ticker": "BTC05M2608060330",
            "series": "BTC05M",
            "category": "Crypto",
            "status": "active",
            "isLive": True,
            "volume": "1127.15",
            "volume24h": "1127.15",
            "template": "crypto-up-down",
            "source": "GRR-KAIKO_RFR_BTCUSD_60S",
            "sourceDetails": {"agency": "Kaiko", "index": "GRR-KAIKO_RFR_BTCUSD_60S"},
            "startTime": "2026-08-06T03:25:00.000Z",
            "expiryDate": "2026-08-06T03:30:00.000Z",
            "contracts": [
                {
                    "id": "179440-431062",
                    "ticker": "UP",
                    "instrumentSymbol": "GEMI-BTC05M2608060330-UP",
                    "status": "active",
                    "marketState": "open",
                    "effectiveDate": "2026-08-06T03:25:00.000Z",
                    "expiryDate": "2026-08-06T03:30:00.000Z",
                    "source": "GRR-KAIKO_RFR_BTCUSD_60S",
                    "strike": {
                        "type": "reference",
                        "value": "64469.05",
                        "availableAt": "2026-08-06T03:25:00.000Z",
                    },
                    "prices": {
                        "buy": {"yes": "0.81", "no": "0.19"},
                        "sell": {"yes": "0.79", "no": "0.17"},
                        "lastTradePrice": "0.80",
                    },
                }
            ],
        }
    ],
    "pagination": {"limit": 1, "offset": 0, "total": 1},
}

LEGACY_EVENTS = {
    "data": [
        {
            "id": "legacy-1",
            "title": "BTC legacy short contract",
            "ticker": "BTC05M2608060340",
            "series": "BTC05M",
            "category": "Crypto",
            "status": "active",
            "isLive": True,
            "volume": "250.00",
            "volume24h": "250.00",
            "contracts": [
                {
                    "id": "legacy-1-up",
                    "ticker": "HI64470D25",
                    "instrumentSymbol": "GEMI-BTC05M2608060340-HI64470D25",
                    "status": "active",
                    "marketState": "open",
                    "effectiveDate": "2026-08-06T03:35:00.000Z",
                    "expiryDate": "2026-08-06T03:40:00.000Z",
                    "prices": {
                        "buy": {"yes": "0.65", "no": "0.35"},
                        "sell": {"yes": "0.61", "no": "0.31"},
                        "lastTradePrice": "0.63",
                    },
                }
            ],
        }
    ]
}

BTCUSD_TICKER = {"bid": "64484.50", "ask": "64485.50", "last": "64485.00", "volume": {"BTC": "1.0"}}


def text_result(text: str, received_at: str = "2026-08-06T03:26:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class GeminiPredictionMarketsAdapterTests(unittest.TestCase):
    def test_parsers_normalize_btc_5m_contract_and_spot_gap(self) -> None:
        spot = parse_gemini_btcusd_ticker(BTCUSD_TICKER, received_at="2026-08-06T03:26:00+00:00")
        rows = parse_gemini_btc_5m_events(
            ACTIVE_EVENTS,
            received_at="2026-08-06T03:26:00+00:00",
            source_url=events_url(10),
            btc_spot=spot,
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("GEMI-BTC05M2608060330-UP", row["inst_id"])
        self.assertEqual("prediction_market", row["market_type"])
        self.assertEqual("prediction_market_probability", row["trade_type"])
        self.assertEqual(0.8, row["last"])
        self.assertEqual(0.79, row["yes_bid"])
        self.assertEqual(0.81, row["yes_ask"])
        self.assertEqual(200.0, row["spread_bps_of_payout"])
        self.assertEqual(64469.05, row["strike_price"])
        self.assertEqual(64485.0, row["underlying_spot_price"])
        self.assertAlmostEqual(15.95, row["spot_strike_gap_usd"], places=6)
        self.assertEqual("buy_yes_event", row["direction"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("open", row["session_status"])
        self.assertEqual(events_url(10), row["source_url"])
        self.assertEqual(BTCUSD_TICKER_URL, row["spot_source_url"])
        self.assertTrue(row["paper_experiment_eligible"])

    def test_parser_accepts_legacy_short_duration_hi_ticker(self) -> None:
        rows = parse_gemini_btc_5m_events(
            LEGACY_EVENTS,
            received_at="2026-08-06T03:36:00+00:00",
            btc_spot={"mid": 64480.0, "source_url": BTCUSD_TICKER_URL},
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("GEMI-BTC05M2608060340-HI64470D25", row["inst_id"])
        self.assertEqual(64470.25, row["strike_price"])
        self.assertEqual(0.63, row["last"])
        self.assertEqual("buy_yes_event", row["direction"])

    def test_scan_preserves_real_rows_when_spot_parser_fails(self) -> None:
        with mock.patch(
            "adapters.venues.gemini_prediction_markets.fetch_text",
            side_effect=[text_result(json.dumps(ACTIVE_EVENTS)), text_result("{}")],
        ):
            batch = GeminiBtcFiveMinutePredictionMarketsAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(1, batch.metadata["real_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["events"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["btc_spot"]["fetch_status"])
        self.assertIn("BTCUSD ticker parser failed", batch.metadata["parser_failures"][0]["error"])
        row = batch.observations[0]
        self.assertEqual("GEMINI", row["venue"])
        self.assertIsNone(row["underlying_spot_price"])
        self.assertEqual("buy_yes_event", row["direction"])
        self.assertTrue(batch.metadata["paper_only"])

    def test_scan_emits_watch_only_health_when_events_are_unavailable(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-06T03:26:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.gemini_prediction_markets.fetch_text",
            side_effect=[blocked, text_result(json.dumps(BTCUSD_TICKER))],
        ):
            batch = GeminiBtcFiveMinutePredictionMarketsAdapter().scan({})

        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["events"]["fetch_status"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual(
            "public_prediction_market_source_unavailable",
            batch.observations[0]["candidate_reject_reason"],
        )
        self.assertIn("status=active", batch.observations[0]["source_url"])

    def test_plugin_discovery_runtime_and_capability_reconciliation(self) -> None:
        adapter_id = "gemini_prediction_markets_btc_5m"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, GeminiBtcFiveMinutePredictionMarketsAdapter)
        self.assertEqual("GEMINI", adapter.info.venue)
        self.assertEqual(DOCS_URL, adapter.info.docs_url)
        self.assertIn("ticker", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_gemini() -> list[str]:
            return [candidate for candidate in original_discover() if candidate == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.gemini_prediction_markets.fetch_text",
            side_effect=[
                text_result(json.dumps(ACTIVE_EVENTS)),
                text_result(json.dumps(BTCUSD_TICKER)),
            ],
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime, "discover_adapters", side_effect=discover_only_gemini
        ):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0, "event_limit": 10}},
                    }
                }
            )

        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(1, report["adapters"][0]["observation_count"])
        self.assertEqual(1170, report["adapters"][0]["adapter_spec_id"])
        self.assertEqual(0, report["adapters"][0]["candidate_count"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #1170: Gemini",
                "market_key": "global_discovery|Gemini",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Gemini",
                        "public_docs_url": DOCS_URL,
                        "asset_or_event": "Gemini prediction markets: BTC 5-minute event contracts",
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])
        self.assertIn("ticker", match["available_capabilities"])


if __name__ == "__main__":
    unittest.main()
