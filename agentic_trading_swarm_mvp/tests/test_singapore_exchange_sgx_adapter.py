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

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.singapore_exchange_sgx import (
    ANNOUNCEMENT_URL,
    DOCS_URL,
    FACTSHEET_URL,
    SingaporeExchangeSgxCryptoPerpetualAdapter,
    parse_sgx_announcement,
    parse_sgx_crypto_perpetual_factsheet,
)


ANNOUNCEMENT_HTML = """
<html><body>
<h1>General Announcement::SGX Derivatives breaks new ground with institutional-grade crypto perpetual futures</h1>
<div>Announcement Details</div>
<div>Date &amp;Time of Broadcast</div>
<div>17-Nov-2025 17:19:26</div>
<div>Description (Please provide a detailed description of the event in the box below)</div>
<p>
Launching on 24 November 2025, these innovative contracts provide a continuous,
no-expiry structure favoured by crypto-native communities.
</p>
<p>Bitcoin and Ethereum perpetual futures will trade on SGX.</p>
</body></html>
"""

FACTSHEET_TEXT = """
SGX Crypto Perpetual Futures
June 2026
BTP DAV (lots) 621 1,296 2,043 1,709 2,526 1,273 1,760
ETP DAV (lots) 466 492 468 466 776 138 79
BTP+ETP DAV (lots) 1,087 1,788 2,511 2,175 3,302 1,411 1,839
BTP OI (lots) 53 264 220 1,162 890 714 707
ETP OI (lots) 24 139 145 257 527 507 518
BTP+ETP OI (lots) 77 403 365 1,419 1,417 1,221 1,225

Contract Specifications: SGX Bitcoin Perpetual Futures
Product Name SGX Bitcoin Perpetual Futures
Underlying Index iEdge CoinDesk Bitcoin Reference Rate Index (BBG: IEBRR Index; Refinitiv: .IEBRR)
Product Code
Contract size 0.2 Bitcoin, as defined by iEdge CoinDesk Bitcoin Reference Rate Index
~US$14,765 (based on index value as of 31 May 2026)
Tick size / Minimum Price Fluctuation PERP 5 index points (US$ 1)
TAS 1 index point (US$ 0.20)
Trading hours (Singapore Time)
Funding Rate mechanism
Funding Rate is computed every minute (from start of T+1 session of preceding business day to end of T session of current business day), published approximately every 5 minutes
PERP TAS
SGX BTP BTPTS
Refinitiv SIMBTCPPFZ9 SIMBTPTSZ49
T Session Pre -Opening : 7.00 am - 7.03 am
Non -Cancel : 7.03 am - 7.05 am
Opening : 7.05 am - 4.00 pm
Pre -Opening : 7.00 am - 7.03 am
Non -Cancel : 7.03 am - 7.05 am
Opening : 7.05 am - 3.00 pm
T+1 Session Opening : 4.05 pm - 5.15 am (next day) Opening : 4.05 pm - 5.15 am (next day)
SGX BTFR
Refinitiv SIMBTCFR

Contract Specifications: SGX Ethereum Perpetual Futures
Product Name SGX Ethereum Perpetual Futures
Underlying Index iEdge CoinDesk Ethereum Reference Rate Index (BBG: IEERR Index; Refinitiv: .IEERR)
Product Code
Contract size 5 Ethereum, as defined by iEdge CoinDesk Ethereum Reference Rate Index
~US$10,113 (based on index value as of 31 May 2026)
Tick size / Minimum Price Fluctuation PERP 0.2 index points (US$ 1)
TAS 0.1 index point (US$ 0.50)
Trading hours (Singapore Time)
Funding Rate mechanism
Funding Rate is computed every minute (from start of T+1 session of preceding business day to end of T session of current business day), published approximately every 5 minutes
PERP TAS
SGX ETP ETPTS
Refinitiv SIMETHPPFZ9 SIMETPTSZ49
T Session Pre -Opening : 7.00 am - 7.03 am
Non -Cancel : 7.03 am - 7.05 am
Opening : 7.05 am - 4.00 pm
Pre -Opening : 7.00 am - 7.03 am
Non -Cancel : 7.03 am - 7.05 am
Opening : 7.05 am - 3.00 pm
T+1 Session Opening : 4.05 pm - 5.15 am (next day) Opening : 4.05 pm - 5.15 am (next day)
SGX ETFR
Refinitiv SIMETHFR
"""


def text_result(text: str, received_at: str = "2026-08-06T03:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


def pdf_result(text: str, received_at: str = "2026-08-06T03:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "content": b"",
        "received_at": received_at,
        "latency_ms": 5.0,
    }


class SingaporeExchangeSgxAdapterTests(unittest.TestCase):
    def test_parsers_normalize_btp_and_etp_contract_references(self) -> None:
        announcement = parse_sgx_announcement(ANNOUNCEMENT_HTML)
        self.assertEqual("2025-11-24", announcement["launch_date"])
        self.assertTrue(announcement["broadcast_at"].startswith("2025-11-17T17:19:26"))

        rows = parse_sgx_crypto_perpetual_factsheet(
            FACTSHEET_TEXT,
            received_at="2026-08-06T03:00:00+00:00",
            announcement=announcement,
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual({"BTP", "ETP"}, set(by_symbol))
        self.assertAlmostEqual(73825.0, by_symbol["BTP"]["last"])
        self.assertAlmostEqual(2022.6, by_symbol["ETP"]["last"])
        self.assertEqual(1760.0, by_symbol["BTP"]["avg_daily_volume_lots"])
        self.assertEqual(518.0, by_symbol["ETP"]["open_interest_lots"])
        self.assertEqual("stale", by_symbol["BTP"]["freshness_state"])
        self.assertEqual("t_session", by_symbol["BTP"]["session_status"])
        self.assertEqual("BTFR", by_symbol["BTP"]["sgx_funding_rate_code"])
        self.assertEqual("ETP", by_symbol["ETP"]["sgx_contract_code"])
        self.assertEqual("2025-11-24", by_symbol["ETP"]["launch_date"])
        self.assertEqual("public_sgx_contract_reference_route_needed", by_symbol["BTP"]["candidate_reject_reason"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))

    def test_scan_preserves_real_rows_and_parser_failure_evidence(self) -> None:
        adapter_id = "singapore_exchange_sgx_crypto_perpetual_futures"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, SingaporeExchangeSgxCryptoPerpetualAdapter)
        self.assertEqual(DOCS_URL, adapter.info.docs_url)
        self.assertIn("event_price_reference", adapter.info.capabilities)

        with mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_text",
            side_effect=[text_result("<html>docs</html>"), text_result(ANNOUNCEMENT_HTML)],
        ), mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_bytes",
            return_value=pdf_result(FACTSHEET_TEXT),
        ):
            batch = SingaporeExchangeSgxCryptoPerpetualAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(2, batch.metadata["real_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["factsheet"]["fetch_status"])
        self.assertEqual("stale", batch.metadata["freshness_state"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        with mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_text",
            side_effect=[text_result("<html>docs</html>"), text_result(ANNOUNCEMENT_HTML)],
        ), mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_bytes",
            return_value=pdf_result("broken factsheet"),
        ):
            degraded = SingaporeExchangeSgxCryptoPerpetualAdapter().scan({})
        self.assertEqual("degraded", degraded.metadata["source_status"])
        self.assertEqual(1, len(degraded.metadata["parser_failures"]))
        self.assertEqual("reachable", degraded.metadata["fetch_status"]["factsheet"]["fetch_status"])
        self.assertTrue(
            any(
                row.get("symbol") == "FACTSHEET_HEALTH"
                and row.get("candidate_reject_reason") == "public_sgx_parser_failure"
                for row in degraded.observations
            )
        )

    def test_runtime_discovery_and_capability_match_cover_spec_834(self) -> None:
        adapter_id = "singapore_exchange_sgx_crypto_perpetual_futures"
        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_text",
            side_effect=[text_result("<html>docs</html>"), text_result(ANNOUNCEMENT_HTML)],
        ), mock.patch(
            "adapters.venues.singapore_exchange_sgx.fetch_bytes",
            return_value=pdf_result(FACTSHEET_TEXT),
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime, "discover_adapters", side_effect=lambda: [item for item in original_discover() if item == adapter_id]
        ):
            runtime_batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )
        report = runtime_batch.metadata["public_market_adapters"]["adapters"][0]
        self.assertEqual(adapter_id, report["adapter_id"])
        self.assertEqual("reachable", report["source_status"])
        self.assertEqual(2, report["observation_count"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #834: Singapore Exchange (SGX)",
                "market_key": "global_discovery|Singapore Exchange (SGX)",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Singapore Exchange (SGX)",
                        "public_docs_url": ANNOUNCEMENT_URL,
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
