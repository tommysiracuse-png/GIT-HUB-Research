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

import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.hacienda_la_esmeralda_esmeralda_auction import (
    AUCTION_URL,
    FAQ_URL,
    HaciendaLaEsmeraldaEsmeraldaAuctionAdapter,
    parse_hacienda_la_esmeralda_esmeralda_auction_faq,
)


FAQ_TEXT = """
<html><body>
<h1>FAQS</h1>
<h2>What is the Esmeralda Auction?</h2>
<p>The Esmeralda Auction is an online green coffee auction held by Hacienda La
Esmeralda in which the farms' best Geisha microlots are available for bidding.</p>
<p>The upcoming Esmeralda Auction will be held on August 18, 2026.</p>
<p>05:00 AM - New York</p>
<p>The minimum bid is $4 per kg.</p>
<p>All auction coffees are green-tip Geisha.</p>
<p>Esmeralda Auction lots come from our three Geisha producing farms: Jaramillo,
El Velo and Cañas Verdes.</p>
<p>Enero: Coffee harvested in January. Carnaval: Coffee harvested in February.
San José: Coffee harvested in March. Pascua: Coffee harvested in April.</p>
</body></html>
"""


def fetch_result(text: str, received_at: str = "2026-08-05T12:00:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


class HaciendaLaEsmeraldaEsmeraldaAuctionAdapterTests(unittest.TestCase):
    def test_plugin_is_discoverable_and_paper_only(self) -> None:
        adapter_id = "hacienda_la_esmeralda_esmeralda_auction"

        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)

        self.assertIsInstance(adapter, HaciendaLaEsmeraldaEsmeraldaAuctionAdapter)
        self.assertEqual(FAQ_URL, adapter.info.docs_url)
        self.assertIn("harvest_month_taxonomy", adapter.info.capabilities)
        self.assertIn("lot_story_reference", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_parser_normalizes_public_auction_schedule_minimum_bid_and_harvest_taxonomy(self) -> None:
        rows = parse_hacienda_la_esmeralda_esmeralda_auction_faq(
            FAQ_TEXT, received_at="2026-08-05T12:00:00+00:00"
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("HACIENDA_LA_ESMERALDA:ESMERALDA_AUCTION:2026-08-18", row["inst_id"])
        self.assertEqual(4.0, row["minimum_bid_usd_per_kg"])
        self.assertEqual("published_minimum_bid_not_market_clearing_price", row["price_basis"])
        self.assertEqual("scheduled", row["session_status"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("2026-08-18T05:00:00-04:00", row["auction_at"])
        self.assertEqual(("Jaramillo", "El Velo", "Cañas Verdes"), row["geisha_farms"])
        self.assertEqual(("Enero", "Carnaval", "San José", "Pascua"), row["harvest_month_labels"])
        self.assertEqual(FAQ_URL, row["source_url"])
        self.assertEqual(AUCTION_URL, row["lot_catalogue_url"])
        self.assertEqual("watch_only", row["direction"])
        self.assertFalse(row["price_available"])
        self.assertEqual("synthetic_research_only", row["paper_route_status"])

    def test_adapter_keeps_fetch_and_parser_failure_evidence(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-05T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.hacienda_la_esmeralda_esmeralda_auction.fetch_text",
            side_effect=[fetch_result("<html>replacement page</html>"), blocked],
        ):
            batch = HaciendaLaEsmeraldaEsmeraldaAuctionAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["faq"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["lot_catalogue"]["fetch_status"])
        self.assertIn("auction markers", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual("public_esmeralda_auction_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual("public_esmeralda_auction_source_unavailable", batch.observations[1]["candidate_reject_reason"])

    def test_adapter_runtime_auto_discovers_normalized_batch(self) -> None:
        adapter_id = "hacienda_la_esmeralda_esmeralda_auction"
        original_discover = adapter_runtime.discover_adapters

        def discover_only_hacienda() -> list[str]:
            return [discovered_id for discovered_id in original_discover() if discovered_id == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.hacienda_la_esmeralda_esmeralda_auction.fetch_text",
            side_effect=[fetch_result(FAQ_TEXT), fetch_result("<html><body>Lot catalogue</body></html>")],
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_hacienda):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual(1, len(batch.observations))
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])


if __name__ == "__main__":
    unittest.main()
