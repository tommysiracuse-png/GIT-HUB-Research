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

import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.hyperliquid_docs import (
    HIP3_DOCS_URL,
    INFO_URL,
    PERPETUALS_DOCS_URL,
    HyperliquidPublicPerpetualsAdapter,
    parse_hyperliquid_funding_history,
    parse_hyperliquid_open_interest_caps,
    parse_hyperliquid_predicted_fundings,
)


OBSERVED_AT = "2026-08-05T12:00:00+00:00"
FUNDING_HISTORY_ETH = [
    {"coin": "ETH", "fundingRate": "0.000001", "premium": "-0.0002", "time": 1785920400000},
    {"coin": "ETH", "fundingRate": "0.000004", "premium": "-0.0001", "time": 1785924000000},
]
FUNDING_HISTORY_AVAX = [
    {"coin": "AVAX", "fundingRate": "-0.000008", "premium": "0.0003", "time": 1785924000000},
]
PREDICTED_FUNDINGS = [
    [
        "ETH",
        [
            ["BinPerp", {"fundingRate": "0.00008", "fundingIntervalHours": 8, "nextFundingTime": 1785927600000}],
            ["HlPerp", {"fundingRate": "0.000004", "fundingIntervalHours": 1, "nextFundingTime": 1785924000000}],
            ["BybitPerp", {"fundingRate": "0.00006", "fundingIntervalHours": 8, "nextFundingTime": 1785927600000}],
        ],
    ],
    [
        "AVAX",
        [
            ["BinPerp", {"fundingRate": "-0.00004", "fundingIntervalHours": 8, "nextFundingTime": 1785927600000}],
            ["HlPerp", {"fundingRate": "-0.000008", "fundingIntervalHours": 1, "nextFundingTime": 1785924000000}],
        ],
    ],
]


def fetch_result(payload: object) -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": json.dumps(payload),
        "received_at": OBSERVED_AT,
        "latency_ms": 4.0,
    }


class HyperliquidDocsAdapterTests(unittest.TestCase):
    def test_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "hyperliquid_public_perpetuals"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("HYPERLIQUID", adapter.info.venue)
        self.assertEqual(PERPETUALS_DOCS_URL, adapter.info.docs_url)
        self.assertIn("public_market_data", adapter.info.capabilities)
        self.assertIn("predicted_fundings", adapter.info.capabilities)
        self.assertIn("hip3_perp_dex_capacity", adapter.info.capabilities)
        self.assertNotIn("order_entry", adapter.info.capabilities)

    def test_parsers_normalize_realized_predicted_and_per_dex_capacity_data(self) -> None:
        history = parse_hyperliquid_funding_history(FUNDING_HISTORY_ETH, coin="ETH", observed_at=OBSERVED_AT)
        predicted = parse_hyperliquid_predicted_fundings(PREDICTED_FUNDINGS, coins={"ETH"}, observed_at=OBSERVED_AT)
        caps = parse_hyperliquid_open_interest_caps(["eth", "AVAX"], dex="xyz", observed_at=OBSERVED_AT, source_url=HIP3_DOCS_URL)

        self.assertIsNotNone(history)
        self.assertEqual(2, history["funding_history_count"])
        self.assertEqual(0.04, history["realized_funding_bps"])
        self.assertEqual("open_24_7", history["session_status"])
        self.assertEqual(PERPETUALS_DOCS_URL, history["source_url"])
        self.assertEqual({"ETH"}, set(predicted))
        self.assertEqual(2, predicted["ETH"]["external_funding_venue_count"])
        self.assertAlmostEqual(0.06, predicted["ETH"]["largest_external_funding_divergence_bps_per_hour"])
        self.assertEqual("watch_only", predicted["ETH"]["direction"])
        self.assertEqual("xyz", caps["perp_dex"])
        self.assertEqual(["AVAX", "ETH"], caps["open_interest_cap_symbols"])
        self.assertEqual(HIP3_DOCS_URL, caps["source_url"])

    def test_runtime_emits_real_observations_with_fetch_and_freshness_evidence(self) -> None:
        with mock.patch(
            "adapters.venues.hyperliquid_docs.fetch_text",
            side_effect=[
                fetch_result(FUNDING_HISTORY_ETH),
                fetch_result(FUNDING_HISTORY_AVAX),
                fetch_result(PREDICTED_FUNDINGS),
                fetch_result(["ETH"]),
                fetch_result(["AVAX"]),
            ],
        ) as fetch:
            batch = HyperliquidPublicPerpetualsAdapter().scan(
                {
                    "public_market_adapters": {
                        "hyperliquid_public_perpetuals": {"perp_dexes": [None, "xyz"]}
                    }
                }
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(4, batch.metadata["real_observation_count"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertFalse(batch.metadata["live_trading_enabled"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["predicted_fundings"]["fetch_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("open_24_7", batch.metadata["session_state"])
        eth = next(row for row in batch.observations if row.get("symbol") == "ETH-PERP")
        self.assertEqual(0.04, eth["realized_funding_bps"])
        self.assertEqual(0.04, eth["predicted_funding_bps"])
        self.assertIn("BinPerp", eth["predicted_funding_venue_rates"])
        self.assertTrue(all(call.args[0] == INFO_URL for call in fetch.call_args_list))
        self.assertEqual("fundingHistory", fetch.call_args_list[0].kwargs["json_body"]["type"])
        self.assertEqual("predictedFundings", fetch.call_args_list[2].kwargs["json_body"]["type"])
        self.assertEqual("perpsAtOpenInterestCap", fetch.call_args_list[3].kwargs["json_body"]["type"])
        self.assertEqual("xyz", fetch.call_args_list[4].kwargs["json_body"]["dex"])
        self.assertEqual(HIP3_DOCS_URL, batch.metadata["fetch_status"]["open_interest_cap:xyz"]["source_url"])

    def test_runtime_auto_discovers_the_plugin(self) -> None:
        adapter_id = "hyperliquid_public_perpetuals"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_hyperliquid() -> list[str]:
            return [found for found in original_discover() if found == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.hyperliquid_docs.fetch_text",
            side_effect=[
                fetch_result(FUNDING_HISTORY_ETH),
                fetch_result(FUNDING_HISTORY_AVAX),
                fetch_result(PREDICTED_FUNDINGS),
                fetch_result([]),
            ],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_hyperliquid):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(3, len(batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_unavailable_sources_remain_watch_only_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": OBSERVED_AT,
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch("adapters.venues.hyperliquid_docs.fetch_text", return_value=blocked):
            batch = HyperliquidPublicPerpetualsAdapter().scan({})

        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual(4, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(all(row["candidate_reject_reason"] == "public_perpetuals_source_unavailable" for row in batch.observations))


if __name__ == "__main__":
    unittest.main()
