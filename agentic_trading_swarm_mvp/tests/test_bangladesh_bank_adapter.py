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
from adapters.venues.bangladesh_bank import (
    AUCTION_CALENDAR_URL,
    PRESS_RELEASE_URL,
    BangladeshBankTreasuryAuctionsAdapter,
    parse_bangladesh_bank_auction_calendar,
    parse_bangladesh_bank_auction_result_links,
    parse_bangladesh_bank_auction_results,
)


CALENDAR = """
<html><body>
  <h2>Treasury Bills Auction Calendar of FY2026-2027</h2>
  <table><tr><th>Auction no</th><th>Auction Date</th><th>14 days</th><th>91 days</th><th>182 days</th><th>364 days</th><th>Total Amount</th></tr>
  <tr><td>6</td><td>09-Aug-2026</td><td>0.00</td><td>3000.00</td><td>2500.00</td><td>2000.00</td><td>7500</td></tr></table>
  <h2>Treasury Bonds (BGTBs) Auction Calendar of FY2026-2027</h2>
  <table><tr><th>Auction no</th><th>Auction Date</th><th>2 yr</th><th>5 yr</th><th>10 yr</th><th>15 yr</th><th>20 yr</th><th>3 yr(FRTB)</th><th>Total Amount</th></tr>
  <tr><td>5</td><td>06-Aug-2026</td><td>5000.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>500.00</td><td>5500</td></tr></table>
</body></html>
"""

LIVE_STYLE_CALENDAR = """
<html><body>
<div class="table_caption">Treasury Bills Auction Calendar</div>
<div class="row-header table_header"><div class="column">Auction no</div><div class="column">Auction Date</div><div class="column">91 days</div><div class="column">182 days</div><div class="column">364 days</div><div class="column">Total Amount</div></div>
<div class="row-data data"><div class="column">6</div><div class="column">09-Aug-2026</div><div class="column">3000.00</div><div class="column">2500.00</div><div class="column">2000.00</div><div class="column">7500</div></div>
</body></html>
"""

PRESS_INDEX = """
<html><body><table>
 <tr><td>1</td><td>03/08/2026</td><td class="text-left">Treasury Bills Auctions held on 02 August 2026 <a pdf-link="https://example.test/bills.pdf">more</a></td></tr>
 <tr><td>2</td><td>29/07/2026</td><td class="text-left">20 Year Bangladesh Govt. Treasury Bond Auction (Re-Issue) Result held on 28 July 2026 <a pdf-link="https://example.test/bond.pdf">more</a></td></tr>
 <tr><td>3</td><td>29/07/2026</td><td>Open Market Operations <a pdf-link="https://example.test/omo.pdf">more</a></td></tr>
</table></body></html>
"""

BILL_RESULT = """
Bangladesh Bank
Treasury Bills Auctions held on 02 August 2026
PARTICULARS OF BILLS AMOUNT TO BE AUCTIONED
91-Days 3000.00 427 10,159.94 97.8054 97.2010 323 3,000.00 29,332,416,440.00 97.7747 9.13 97.7339 9.30
182-Days 2500.00 306 9,173.97 95.5045 94.5099 206 2,500.00 23,870,778,105.30 95.4831 9.49 95.4636 9.53
364-Days 2000.00 285 10,449.96 91.3792 89.5112 138 2,000.00 18,267,574,482.70 91.3379 9.51 91.3168 9.54
"""

BOND_RESULT = """
Bangladesh Bank
20 Year Bangladesh Govt. Treasury Bond Auction Result held on 28 July 2026
20-YEAR BGTB 1000.00 128 3167.41 10.28 11.94 55 1000.00 9,356,995,733.60 9.20 10.34 10.32
"""


def text_result(text: str, received_at: str = "2026-08-04T08:30:00+00:00") -> dict:
    return {"ok": True, "status": "reachable", "http_status": 200, "text": text, "received_at": received_at, "latency_ms": 4.0}


class BangladeshBankAdapterTests(unittest.TestCase):
    def test_calendar_and_result_parsers_normalize_supported_tenors(self) -> None:
        calendar = parse_bangladesh_bank_auction_calendar(CALENDAR, received_at="2026-08-04T08:30:00+00:00")
        by_symbol = {row["symbol"]: row for row in calendar}
        self.assertEqual({"TBILL_91D", "TBILL_182D", "TBILL_364D", "BGTB_2Y", "3Y_FRTB"}, set(by_symbol))
        self.assertEqual(3000.0, by_symbol["TBILL_91D"]["announced_supply_crore_bdt"])
        self.assertEqual(500.0, by_symbol["3Y_FRTB"]["announced_supply_crore_bdt"])
        self.assertEqual("auction_scheduled", by_symbol["BGTB_2Y"]["session_status"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in calendar))
        self.assertTrue(all(not row["paper_experiment_eligible"] for row in calendar))
        self.assertEqual(3, len(parse_bangladesh_bank_auction_calendar(LIVE_STYLE_CALENDAR)))

        bill_rows = parse_bangladesh_bank_auction_results(BILL_RESULT, source_url="https://example.test/bills.pdf", received_at="2026-08-04T08:30:00+00:00")
        bill = {row["symbol"]: row for row in bill_rows}["TBILL_91D"]
        self.assertEqual(9.13, bill["auction_weighted_average_yield_pct"])
        self.assertEqual(9.30, bill["auction_stop_out_yield_pct"])
        self.assertAlmostEqual(10_159.94 / 3000.0, bill["auction_coverage_ratio"], places=6)
        self.assertEqual(97.7747, bill["auction_weighted_average_price_per_100"])
        self.assertTrue(bill["paper_experiment_eligible"])

        bond = parse_bangladesh_bank_auction_results(BOND_RESULT, source_url="https://example.test/bond.pdf", received_at="2026-08-04T08:30:00+00:00")[0]
        self.assertEqual("BGTB_20Y", bond["symbol"])
        self.assertEqual(10.32, bond["last"])
        self.assertEqual(9.2, bond["coupon_rate_pct"])
        self.assertEqual(3.16741, bond["auction_coverage_ratio"])

    def test_result_index_parser_uses_only_treasury_result_pdfs(self) -> None:
        links = parse_bangladesh_bank_auction_result_links(PRESS_INDEX)
        self.assertEqual(["https://example.test/bills.pdf", "https://example.test/bond.pdf"], [item["source_url"] for item in links])

    def test_runtime_discovery_scan_and_failure_evidence(self) -> None:
        adapter_id = "bangladesh_bank_treasury_auctions"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, BangladeshBankTreasuryAuctionsAdapter)
        self.assertEqual(AUCTION_CALENDAR_URL, adapter.info.docs_url)
        self.assertIn("auction_results", adapter.info.capabilities)

        with mock.patch("adapters.venues.bangladesh_bank.fetch_text", side_effect=[text_result(CALENDAR), text_result(PRESS_INDEX)]), mock.patch(
            "adapters.venues.bangladesh_bank.fetch_bytes", side_effect=[text_result(BILL_RESULT), text_result(BOND_RESULT)]
        ):
            batch = BangladeshBankTreasuryAuctionsAdapter().scan({"public_market_adapters": {"bangladesh_bank_treasury_auctions": {"max_result_documents": 2}}})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["auction_calendar"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["auction_result_2"]["fetch_status"])
        self.assertEqual(2, batch.metadata["result_document_count"])
        self.assertEqual(9, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        original_discover = adapter_runtime.discover_adapters
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.bangladesh_bank.fetch_text", side_effect=[text_result(CALENDAR), text_result(PRESS_INDEX)]
        ), mock.patch(
            "adapters.venues.bangladesh_bank.fetch_bytes", side_effect=[text_result(BILL_RESULT), text_result(BOND_RESULT)]
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
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0, "max_result_documents": 2}}}}
            )
        self.assertEqual(adapter_id, runtime_batch.metadata["public_market_adapters"]["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", runtime_batch.metadata["public_market_adapters"]["adapters"][0]["source_status"])

        with mock.patch("adapters.venues.bangladesh_bank.fetch_text", side_effect=[text_result("<html>replacement</html>"), text_result("<html>replacement</html>")]):
            failed = BangladeshBankTreasuryAuctionsAdapter().scan({})
        self.assertEqual("degraded", failed.metadata["source_status"])
        self.assertEqual(2, len(failed.metadata["parser_failures"]))
        self.assertTrue(all(row["direction"] == "watch_only" for row in failed.observations))
        self.assertTrue(all(row["candidate_reject_reason"] == "public_bangladesh_bank_parser_failure" for row in failed.observations))

    def test_capability_reconciliation_matches_spec_293(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #293: Bangladesh Bank",
                "market_key": "global_discovery|Bangladesh Bank",
                "spec": {"candidate": {"venue_or_source": "Bangladesh Bank", "public_docs_url": AUCTION_CALENDAR_URL, "data_access_type": "public_no_key"}},
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("bangladesh_bank_treasury_auctions", match["adapter_id"])


if __name__ == "__main__":
    unittest.main()
