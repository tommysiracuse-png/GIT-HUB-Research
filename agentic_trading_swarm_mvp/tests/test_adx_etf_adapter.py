from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.abu_dhabi_securities_exchange_adx import (
    CATALOG_URL,
    CHADX15_FACTSHEET_URL,
    UAED_FACTSHEET_URL,
    AbuDhabiSecuritiesExchangeAdxEtfAdapter,
    parse_adx_etf_factsheet,
)


CHADX15_TEXT = """
Lunate FTSE ADX 15 ETF - Income (CHADX15)
KEY FACTS
ISIN AEC01137C226
Dividend Treatment Distributing
Domicile UAE
Methodology Replicating
Product Structure Physical
NAV (AED) 3.598
Type Umbrella
DEALING INFORMATION
Exchange Abu Dhabi Securities Exchange
Ticker CHADX15
Trading Currency AED
Trading Hours 10am - 3pm GST
Lunate FTSE ADX 15 ETF - Inc.
JUNE 2026 FACT SHEET
"""

UAED_TEXT = """
Chimera S&P UAE UCITS ETF - Income (UAED)
KEY FACTS
ISIN IE00BKDMN700
Dividend Treatment Distributing
Domicile Ireland
Methodology Replicating
Product Structure Physical
NAV (AED) 5.304
Type UCITS
DEALING INFORMATION
Exchange Abu Dhabi Securities Exchange
Ticker UAED
Trading Currency AED
Trading Hours 10am - 3pm GST
Chimera S&P UAE UCITS ETF - Inc.
MARCH 2026 FACT SHEET
"""


def fetch_result(text: str, received_at: str = "2026-08-04T08:30:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "content_type": "application/pdf",
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class AdxEtfAdapterTests(unittest.TestCase):
    def test_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "abu_dhabi_securities_exchange_adx_etf"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("ADX", adapter.info.venue)
        self.assertEqual(CATALOG_URL, adapter.info.docs_url)
        self.assertIn("net_asset_value", adapter.info.capabilities)
        self.assertIn("product_structure", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_capability_reconciliation_closes_public_factsheet_spec_only(self) -> None:
        spec = {
            "title": "Implement public adapter #654: Abu Dhabi Securities Exchange (ADX)",
            "market_key": "global_discovery|Abu Dhabi Securities Exchange (ADX)",
            "spec": {
                "candidate": {
                    "venue_or_source": "Abu Dhabi Securities Exchange (ADX)",
                    "asset_or_event": "CHADX15 and UAED ETF NAV, ISIN, and structure",
                    "public_docs_url": CATALOG_URL,
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("abu_dhabi_securities_exchange_adx_etf", match["adapter_id"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_parser_normalizes_chadx15_nav_isin_structure_and_state(self) -> None:
        row = parse_adx_etf_factsheet(
            CHADX15_TEXT,
            expected_symbol="CHADX15",
            source_url=CHADX15_FACTSHEET_URL,
            received_at="2026-08-04T08:30:00+00:00",
        )

        self.assertEqual("ADX:ETF:CHADX15", row["inst_id"])
        self.assertEqual(3.598, row["last"])
        self.assertEqual(3.598, row["nav"])
        self.assertEqual("AEC01137C226", row["isin"])
        self.assertEqual("Physical", row["product_structure"])
        self.assertEqual("Replicating", row["replication_methodology"])
        self.assertEqual("Umbrella", row["fund_type"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("open", row["session_status"])
        self.assertEqual("reachable", row["fetch_status"])
        self.assertEqual(CHADX15_FACTSHEET_URL, row["source_url"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual("factsheet_nav_not_entry_quality_quote", row["candidate_reject_reason"])

    def test_adapter_emits_both_real_factsheet_observations(self) -> None:
        with mock.patch(
            "adapters.venues.abu_dhabi_securities_exchange_adx.fetch_bytes",
            side_effect=[fetch_result(CHADX15_TEXT), fetch_result(UAED_TEXT)],
        ) as fetch:
            batch = AbuDhabiSecuritiesExchangeAdxEtfAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(2, batch.metadata["real_observation_count"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual("mixed", batch.metadata["freshness_state"])
        self.assertEqual("open", batch.metadata["session_state"])
        self.assertTrue(batch.metadata["paper_only"])
        by_symbol = {row["symbol"]: row for row in batch.observations}
        self.assertEqual(5.304, by_symbol["UAED"]["nav"])
        self.assertEqual("IE00BKDMN700", by_symbol["UAED"]["isin"])
        self.assertEqual("UCITS", by_symbol["UAED"]["fund_type"])
        self.assertEqual("stale", by_symbol["UAED"]["freshness_state"])
        self.assertEqual(CHADX15_FACTSHEET_URL, fetch.call_args_list[0].args[0])
        self.assertEqual(UAED_FACTSHEET_URL, fetch.call_args_list[1].args[0])

    def test_adapter_preserves_parser_failure_and_fetch_status(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "content": b"",
            "content_type": "",
            "received_at": "2026-08-04T08:30:01+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.abu_dhabi_securities_exchange_adx.fetch_bytes",
            side_effect=[fetch_result("an unrelated PDF"), blocked],
        ):
            batch = AbuDhabiSecuritiesExchangeAdxEtfAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["CHADX15"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["UAED"]["fetch_status"])
        self.assertIn("expected Ticker CHADX15", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual("public_factsheet_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual("public_factsheet_source_unavailable", batch.observations[1]["candidate_reject_reason"])
        self.assertEqual(CHADX15_FACTSHEET_URL, batch.observations[0]["source_url"])
        self.assertEqual(UAED_FACTSHEET_URL, batch.observations[1]["source_url"])


if __name__ == "__main__":
    unittest.main()
