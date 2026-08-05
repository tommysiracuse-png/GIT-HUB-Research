from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from adapters.registry import discover_adapters, get_adapter
import adapter_runtime
from adapters.venues.abu_dhabi_securities_exchange_adx import (
    DERIVATIVES_CLEARING_URL,
    DERIVATIVES_NEWS_URL,
    DERIVATIVES_URL,
    AbuDhabiSecuritiesExchangeAdxDerivativesAdapter,
    parse_adx_derivatives_catalog,
    parse_adx_six_ssf_announcement,
)


CATALOG_TEXT = """
ADX DERIVATIVES MARKET
The single stock futures (SSF) are based on leading blue-chip companies.
ADX also trades FADX 15 index futures based on the FADX 15 index.
All derivative contracts on ADX are settled in cash by the ADClear.
ADX Derivatives Types: Single Stock Futures (SSF), Index Futures.
Active Contracts: 16 SSF contracts and 1 Index Future.
Contract Size: 100 shares per SSF; 1 AED x Index for FADX15.
"""

ANNOUNCEMENT_TEXT = """
ADX Lists Six New Single Stock Futures, Deepens Bloomberg Collaboration
13 Jul 2026
ADX listed six new Single Stock Futures — on ADNOC Gas, ADNOC Drilling,
ADNOC Logistics & Services, Presight AI, Sharjah Islamic Bank, and Two Point
Zero Group. All contracts are cash-settled and centrally cleared by Abu Dhabi
Clear (AD Clear).
"""

CLEARING_TEXT = """
Derivatives Market
Abu Dhabi Clear LLC (AD Clear) offers Clearing and Settlement services to its
Clearing Members in the derivatives market. AD Clear serves as the central
counterparty to both buyers and sellers. Clearing Members deposit collateral.
"""


def fetch_result(text: str, received_at: str = "2026-08-04T08:30:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class AdxDerivativesAdapterTests(unittest.TestCase):
    def test_plugin_is_runtime_discoverable_and_explicitly_paper_only(self) -> None:
        adapter_id = "abu_dhabi_securities_exchange_adx_derivatives"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("ADX", adapter.info.venue)
        self.assertEqual(DERIVATIVES_URL, adapter.info.docs_url)
        self.assertIn("contract_catalog", adapter.info.capabilities)
        self.assertIn("single_stock_futures", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_parsers_normalize_fadx15_and_all_six_announced_ssfs(self) -> None:
        index_rows = parse_adx_derivatives_catalog(
            CATALOG_TEXT,
            received_at="2026-08-04T08:30:00+00:00",
        )
        ssf_rows = parse_adx_six_ssf_announcement(
            ANNOUNCEMENT_TEXT,
            received_at="2026-08-04T08:30:00+00:00",
        )

        self.assertEqual(1, len(index_rows))
        self.assertEqual("ADX:FUTURES:FADX15", index_rows[0]["inst_id"])
        self.assertEqual(1.0, index_rows[0]["contract_multiplier_aed_per_index_point"])
        self.assertEqual("reference_static", index_rows[0]["freshness_state"])
        self.assertEqual("unknown", index_rows[0]["session_status"])
        self.assertEqual(DERIVATIVES_URL, index_rows[0]["source_url"])
        self.assertEqual(6, len(ssf_rows))
        self.assertEqual(
            {"SSF_ADNOC_GAS", "SSF_ADNOC_DRILLING", "SSF_ADNOC_LOGISTICS_SERVICES", "SSF_PRESIGHT_AI", "SSF_SHARJAH_ISLAMIC_BANK", "SSF_TWO_POINT_ZERO_GROUP"},
            {row["symbol"] for row in ssf_rows},
        )
        self.assertTrue(all(row["contract_size_shares"] == 100 for row in ssf_rows))
        self.assertTrue(all(row["direction"] == "watch_only" for row in [*index_rows, *ssf_rows]))
        self.assertTrue(all(row["candidate_reject_reason"] for row in [*index_rows, *ssf_rows]))
        self.assertEqual("listed", ssf_rows[0]["session_status"])
        self.assertEqual(DERIVATIVES_NEWS_URL, ssf_rows[0]["source_url"])

    def test_runtime_emits_real_catalog_rows_and_fetch_evidence(self) -> None:
        with mock.patch(
            "adapters.venues.abu_dhabi_securities_exchange_adx.fetch_text",
            side_effect=[
                fetch_result(CATALOG_TEXT),
                fetch_result(ANNOUNCEMENT_TEXT),
                fetch_result(CLEARING_TEXT),
            ],
        ) as fetch:
            batch = AbuDhabiSecuritiesExchangeAdxDerivativesAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(7, batch.metadata["real_observation_count"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["catalog"]["fetch_status"])
        self.assertEqual("mixed", batch.metadata["freshness_state"])
        self.assertEqual("mixed", batch.metadata["session_state"])
        self.assertEqual(DERIVATIVES_URL, fetch.call_args_list[0].args[0])
        self.assertEqual(DERIVATIVES_NEWS_URL, fetch.call_args_list[1].args[0])
        self.assertEqual(DERIVATIVES_CLEARING_URL, fetch.call_args_list[2].args[0])

    def test_adapter_runtime_auto_discovers_the_derivatives_plugin(self) -> None:
        adapter_id = "abu_dhabi_securities_exchange_adx_derivatives"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_adx_derivatives() -> list[str]:
            return [
                discovered_id
                for discovered_id in original_discover()
                if discovered_id == adapter_id
            ]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.abu_dhabi_securities_exchange_adx.fetch_text",
            side_effect=[
                fetch_result(CATALOG_TEXT),
                fetch_result(ANNOUNCEMENT_TEXT),
                fetch_result(CLEARING_TEXT),
            ],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime, "discover_adapters", side_effect=discover_only_adx_derivatives
        ):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(7, len(batch.observations))
        self.assertEqual([], batch.candidates)
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_runtime_keeps_unavailable_source_as_watch_only_health_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T08:30:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.abu_dhabi_securities_exchange_adx.fetch_text",
            side_effect=[blocked, blocked, blocked],
        ):
            batch = AbuDhabiSecuritiesExchangeAdxDerivativesAdapter().scan({})

        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual(3, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(
            all(row["candidate_reject_reason"] == "public_derivatives_source_unavailable" for row in batch.observations)
        )


if __name__ == "__main__":
    unittest.main()
