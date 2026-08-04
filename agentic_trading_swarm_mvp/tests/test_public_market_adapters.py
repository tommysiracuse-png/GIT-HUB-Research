from __future__ import annotations

import copy
import json
import pathlib
import ssl
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_implementation_owner
import adapter_runtime
import code_evolution
import storage
from adapters.venues import common as venue_common
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.bahrain_cross_listings import cross_listing_observations
from adapters.venues.bursa_derivatives import contract_observations
from adapters.venues.european_energy_exchange_eex import (
    AUCTION_URL as EEX_AUCTION_URL,
    SALES_URL as EEX_SALES_URL,
    EexGermanNehsAdapter,
    parse_eex_nehs_auction,
    parse_eex_nehs_sales,
)
from adapters.venues.e_auksion_district_hokimiyat_notices import (
    API_URL as E_AUKSION_API_URL,
    NOTICE_URL as E_AUKSION_NOTICE_URL,
    EAuksionDistrictHokimiyatNoticesAdapter,
    parse_e_auksion_lots,
)
from adapters.venues.kase_futures import parse_kase_futures
from adapters.venues.norwegian_block_exchange_nbx import (
    DOCS_URL as NBX_DOCS_URL,
    NorwegianBlockExchangeNbxAdapter,
    market_order_book_url,
    parse_nbx_order_book,
    parse_nbx_markets,
)
from adapters.venues.nzx_dairy import parse_nzx_gdt
from adapters.venues.twse_daily import parse_twse_daily
from scan_batch import ScanBatch
from settings import DEFAULT_SETTINGS


class PublicAdapterParserTests(unittest.TestCase):
    def test_public_fetch_retries_certificate_failure_with_system_trust(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return b"[]"

        certificate_error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        system_context = mock.sentinel.system_context
        with mock.patch.object(
            venue_common.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError(certificate_error), Response()],
        ) as urlopen, mock.patch.object(
            venue_common,
            "_system_trust_context",
            return_value=system_context,
        ):
            result = venue_common.fetch_text("https://official.example.test/market-data")

        self.assertTrue(result["ok"])
        self.assertEqual("system", result["tls_trust_source"])
        self.assertEqual(2, urlopen.call_count)
        self.assertIs(system_context, urlopen.call_args_list[1].kwargs["context"])

    def test_public_fetch_supports_no_key_json_post(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return b'{"rows": []}'

        with mock.patch.object(
            venue_common.urllib.request,
            "urlopen",
            return_value=Response(),
        ) as urlopen:
            result = venue_common.fetch_text(
                "https://official.example.test/lots",
                method="POST",
                json_body={"category": 46},
            )

        request = urlopen.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual("POST", request.get_method())
        self.assertEqual(b'{"category":46}', request.data)
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_registered_batch_is_discoverable(self) -> None:
        expected = {
            "twse_daily_public",
            "kase_futures_public_results",
            "nzx_gdt_event_reference",
            "bursa_derivatives_contract_catalog",
            "bahrain_cross_listings_catalog",
            "eex_german_nehs_public",
            "e_auksion_district_hokimiyat_notices",
            "norwegian_block_exchange_nbx_public",
        }
        self.assertTrue(expected <= set(discover_adapters()))

    def test_nbx_plugin_is_runtime_discoverable_and_public_watch_only(self) -> None:
        self.assertIn("norwegian_block_exchange_nbx_public", discover_adapters())
        adapter = get_adapter("norwegian_block_exchange_nbx_public")
        self.assertIsNotNone(adapter)
        self.assertEqual("NBX", adapter.info.venue)
        self.assertIn("order_book", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)
        self.assertEqual(NBX_DOCS_URL, adapter.info.docs_url)

    def test_nbx_order_book_parser_aggregates_depth_and_preserves_freshness(self) -> None:
        rows = [
            {"id": "public-1", "price": "1200.00", "quantity": "0.25", "side": "BUY"},
            {"id": "public-2", "price": "1200.00", "quantity": "0.75", "side": "BUY"},
            {"id": "public-3", "price": "1199.00", "quantity": "2", "side": "BUY"},
            {"id": "public-4", "price": "1202.00", "quantity": "0.5", "side": "SELL"},
            {"id": "public-5", "price": "1203.00", "quantity": "1", "side": "SELL"},
            {"id": "ignored", "price": "0", "quantity": "1", "side": "BUY"},
        ]
        source_url = market_order_book_url("FGLD-NOK")
        row = parse_nbx_order_book(
            rows,
            market="fGLD/NOK",
            received_at="2026-08-04T12:00:00+00:00",
            source_url=source_url,
        )

        self.assertEqual("NBX:FGLD-NOK", row["inst_id"])
        self.assertEqual("tokenized_precious_metal", row["asset_class"])
        self.assertEqual(1201.0, row["last"])
        self.assertEqual(1200.0, row["bid"])
        self.assertEqual(1202.0, row["ask"])
        self.assertAlmostEqual(16.653, row["spread_bps"], places=3)
        self.assertEqual([1200.0, 1.0], row["book_levels"]["bids"][0])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("response_received", row["freshness_basis"])
        self.assertEqual("open_24_7", row["session_status"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(source_url, row["source_url"])

    def test_nbx_market_catalog_parser_selects_active_nordic_fiat_pairs(self) -> None:
        payload = [
            {"id": "BTC-NOK", "quoteAsset": "NOK", "status": "OK", "disabled": False},
            {"id": "ADA-DKK", "quoteAsset": "DKK", "status": "OK", "disabled": False},
            {"id": "FGLD-EUR", "quoteAsset": "EUR", "status": "OK", "disabled": False},
            {"id": "BTC-USDM", "quoteAsset": "USDM", "status": "OK", "disabled": False},
            {"id": "OLD-SEK", "quoteAsset": "SEK", "status": "OK", "disabled": True},
            {"id": "MATIC-EUR", "quoteAsset": "EUR", "status": "OK", "cancelOnly": True},
        ]

        self.assertEqual(["ADA-DKK", "BTC-NOK", "FGLD-EUR"], parse_nbx_markets(payload))

    def test_nbx_adapter_emits_real_books_and_watch_only_unavailable_evidence(self) -> None:
        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": json.dumps(
                [
                    {"price": "1200", "quantity": "1", "side": "BUY"},
                    {"price": "1202", "quantity": "1", "side": "SELL"},
                ]
            ),
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:00:01+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }

        def fake_fetch(url, _timeout):
            return reachable if "FGLD-NOK" in url else blocked

        settings = {
            "public_market_adapters": {
                "norwegian_block_exchange_nbx_public": {
                    "markets": ["FGLD-NOK", "BTC-NOK"],
                    "workers": 1,
                }
            }
        }
        with mock.patch(
            "adapters.venues.norwegian_block_exchange_nbx.fetch_text",
            side_effect=fake_fetch,
        ):
            batch = NorwegianBlockExchangeNbxAdapter().scan(settings)

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(1, batch.metadata["real_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["FGLD-NOK"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["BTC-NOK"]["fetch_status"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        real = next(row for row in batch.observations if row["inst_id"] == "NBX:FGLD-NOK")
        health = next(row for row in batch.observations if row["inst_id"] == "NBX:BTC-NOK:ADAPTER_HEALTH")
        self.assertEqual("https://api.nbx.com/markets/FGLD-NOK/orders", real["source_url"])
        self.assertEqual("public_order_book_source_unavailable", health["candidate_reject_reason"])

    def test_nbx_adapter_preserves_reachable_parser_failure(self) -> None:
        malformed = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": '{"orders": []}',
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        settings = {
            "public_market_adapters": {
                "adapters": {
                    "norwegian_block_exchange_nbx_public": {"markets": ["FSLVR-EUR"]}
                }
            }
        }
        with mock.patch(
            "adapters.venues.norwegian_block_exchange_nbx.fetch_text",
            return_value=malformed,
        ):
            batch = NorwegianBlockExchangeNbxAdapter().scan(settings)

        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["FSLVR-EUR"]["fetch_status"])
        self.assertIn("must be an array", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("public_order_book_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertIn("must be an array", batch.observations[0]["parser_failure"])

    def test_twse_daily_parser_normalizes_official_row(self) -> None:
        rows = parse_twse_daily(
            [
                {
                    "Date": "1150730",
                    "Code": "2330",
                    "Name": "TSMC",
                    "OpeningPrice": "1000",
                    "HighestPrice": "1010",
                    "LowestPrice": "990",
                    "ClosingPrice": "1005",
                    "TradeVolume": "100",
                    "TradeValue": "100500",
                    "Change": "+5",
                }
            ]
        )
        self.assertEqual("TWSE:2330", rows[0]["inst_id"])
        self.assertEqual("TWD", rows[0]["quote"])
        self.assertEqual(1005.0, rows[0]["last"])

    def test_kase_futures_parser_normalizes_table(self) -> None:
        html = """
        <table><tr><th>Instrument</th><th>Settlement</th><th>Min</th><th>Max</th><th>Last</th>
        <th>Volume</th><th>Deals</th><th>Open positions</th><th>Demand</th><th>Ask</th></tr>
        <tr><td>US-9.26</td><td>520,10</td><td>519,00</td><td>521,00</td><td>520,50</td>
        <td>10,5</td><td>4</td><td>7</td><td>520,40</td><td>520,60</td></tr></table>
        """
        rows = parse_kase_futures(html)
        self.assertEqual("KASE:US-9.26", rows[0]["inst_id"])
        self.assertEqual(520.5, rows[0]["last"])
        self.assertEqual("fx_futures", rows[0]["asset_class"])

    def test_nzx_gdt_parser_normalizes_event_prices(self) -> None:
        html = """
        <table><tr><th>Products</th><th>Event 401</th><th>Event 400</th><th>Change</th></tr>
        <tr><td>Whole Milk Powder</td><td>3,900</td><td>3,800</td><td>+2.6%</td></tr></table>
        """
        rows = parse_nzx_gdt(html)
        self.assertEqual("NZX_GDT:WHOLE_MILK_POWDER", rows[0]["inst_id"])
        self.assertEqual(3900.0, rows[0]["last"])
        self.assertEqual("Event 401", rows[0]["event_id"])

    def test_eex_nehs_auction_parser_normalizes_official_result(self) -> None:
        report = """\ufeffDatum/Date;Zeit/Time (UTC);Versteigerung/Auction;Fälligkeit/Vintage;Zuschlagspreis/Auction clearing price (EUR/nEZ);versteigerte Menge/Volume allocated in the auction (in nEZ);verbleibende Gesamtversteigerungsmenge/Total remaining auction volume (in nEZ);Gesamtgebotsmenge/Total volume of bids (in nEZ);Gesamtzahl der Bieter/Total number of bidders;Gesamtzahl erfolgreicher Bieter/Total number of successful bidders;Zertifikatserlöse/Revenues (in EUR);Cover ratio/cover ratio;Cover ratio (basierend auf der tatsächlich zugeteilten Menge)/Cover ratio (based on actual allocated volume);Information über potenzielle Annullierung der Versteigerung/Information on potential cancellation of an auction
29.07.2026;13:00;nEZ;2026;65.00;21341544;85377434;515902199;110;110;1387200360.00;48.35;24.17;
"""
        rows = parse_eex_nehs_auction(
            report,
            received_at="2026-07-30T13:00:00+00:00",
        )
        self.assertEqual("EEX:NEZ_2026:AUCTION:2026-07-29", rows[0]["inst_id"])
        self.assertEqual(65.0, rows[0]["last"])
        self.assertEqual(21_341_544.0, rows[0]["allocated_volume"])
        self.assertEqual(24.17, rows[0]["allocated_volume_cover_ratio"])
        self.assertEqual("fresh", rows[0]["freshness_state"])
        self.assertEqual("closed", rows[0]["session_status"])
        self.assertEqual(EEX_AUCTION_URL, rows[0]["source_url"])

    def test_eex_nehs_sales_parser_skips_disclaimer_preamble(self) -> None:
        report = """\ufeff"Disclaimer: Results marked with * are preliminary."

Datum/Date;Zeit/Time;Verkauf/Sale;Fälligkeit/Vintage;Verkaufspreis/Price €/tCO2;Verkaufsvolumen/Volume tCO2;Anzahl der Handelsgeschäfte/Number of trades;Anzahl der Käufer/Number of Buyers;Zertifikatserlös/Certificate-revenue €;Disclaimer
28.07.2026;13:15;nEZ;2025;55;216421;37;17;11903155;
"""
        rows = parse_eex_nehs_sales(
            report,
            received_at="2026-07-29T13:15:00+00:00",
        )
        self.assertEqual("EEX:NEZ_2025:SALE:2026-07-28", rows[0]["inst_id"])
        self.assertEqual(216_421.0, rows[0]["sold_volume"])
        self.assertEqual(37, rows[0]["transaction_count"])
        self.assertEqual(17, rows[0]["buyer_count"])
        self.assertEqual("final", rows[0]["result_finality"])
        self.assertEqual(EEX_SALES_URL, rows[0]["source_url"])

    def test_eex_adapter_preserves_parser_failure_and_source_health(self) -> None:
        auction_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "not,the,documented,schema",
            "received_at": "2026-07-30T13:00:00+00:00",
            "latency_ms": 5.0,
        }
        sales_result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-07-30T13:00:01+00:00",
            "latency_ms": 6.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            side_effect=[auction_result, sales_result],
        ):
            batch = EexGermanNehsAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["auction"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["sales"]["fetch_status"])
        self.assertIn("header", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))

    def test_e_auksion_parser_normalizes_land_lot_schedule_and_price_terms(self) -> None:
        payload = {
            "totalPages": 16,
            "totalRows": 188,
            "currentPage": 1,
            "rows": [
                {
                    "id": 24649756,
                    "lot_number": "24649756",
                    "name": "MG1735228008/27 yer uchastkasi",
                    "full_address": (
                        "Qoraqalpog`iston Respublikasi, Taxiatosh tumani, Oydin yo'l MFY"
                    ),
                    "confiscant_categories_name": "Tadbirkorlik va shaharsozlik uchun",
                    "category_id": 46,
                    "start_price": 4_215_000.0,
                    "zaklad_summa": 1_053_750.0,
                    "auction_date_str": "10.08.2026 10:00",
                    "order_end_time_str": "10.08.2026 09:00",
                    "zaklad_percent": 25.0,
                    "lot_statuses_id": 2,
                    "is_term_payment": 1,
                    "term_month": 60,
                    "baholangan_narx": 6_804_402.0,
                    "user_orders_apply_cnt": 3,
                    "view_count": 9,
                }
            ],
        }
        rows = parse_e_auksion_lots(
            payload,
            received_at="2026-08-04T05:30:00+00:00",
        )

        row = rows[0]
        self.assertEqual("E_AUKSION_UZ:LAND_LEASE:24649756", row["inst_id"])
        self.assertEqual(4_215_000.0, row["last"])
        self.assertEqual(1_053_750.0, row["deposit_uzs"])
        self.assertEqual("Taxiatosh tumani", row["district"])
        self.assertEqual("2026-08-10T10:00:00+05:00", row["auction_at"])
        self.assertEqual("applications_open", row["session_status"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual(E_AUKSION_API_URL, row["source_url"])
        self.assertEqual(E_AUKSION_NOTICE_URL, row["source_notice_url"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual("land_lease_auction_not_order_routable", row["candidate_reject_reason"])

    def test_e_auksion_adapter_preserves_parser_failure_and_fetch_evidence(self) -> None:
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": '{"unexpected": []}',
            "received_at": "2026-08-04T05:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.e_auksion_district_hokimiyat_notices.fetch_text",
            return_value=result,
        ) as fetch:
            batch = EAuksionDistrictHokimiyatNoticesAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["lots"]["fetch_status"])
        self.assertIn("rows array", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual("public_reference_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertIn("rows array", batch.observations[0]["parser_failure"])
        self.assertEqual("POST", fetch.call_args.kwargs["method"])
        self.assertEqual(46, fetch.call_args.kwargs["json_body"]["confiscant_categories_id"])

    def test_catalog_adapters_never_invent_prices(self) -> None:
        rows = contract_observations("blocked") + cross_listing_observations("reachable")
        self.assertTrue(rows)
        self.assertTrue(all(row["last"] == 0.0 for row in rows))
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["candidate_reject_reason"] == "public_quote_endpoint_not_available" for row in rows))

    def test_runtime_combines_registered_batches(self) -> None:
        class FakeAdapter:
            class info:
                adapter_id = "fake_public"
                venue = "FAKE"
                market_type = "equity"
                source = "fixture"
                active = True
                default_cache_minutes = 0
                runtime_entrypoint = "fake.scan"
                docs_url = "https://example.test"

            def scan(self, _settings):
                return ScanBatch(
                    source="fixture",
                    candidates=[],
                    observations=[{"venue": "FAKE", "inst_id": "FAKE:X", "last": 1.0}],
                    metadata={"source_status": "reachable"},
                )

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["public_market_adapters"] = {"enabled": True, "workers": 1}
        fake = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache")
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", return_value=["fake_public"]), mock.patch.object(
            adapter_runtime, "get_adapter", return_value=fake
        ):
            batch = adapter_runtime.build_scan_batch(settings)
        self.assertEqual(1, len(batch.observations))
        self.assertEqual("FAKE:X", batch.observations[0]["inst_id"])
        self.assertEqual({"FAKE": 1}, batch.metadata["public_market_adapters"]["summary"]["observations_by_venue"])
        inventory = batch.metadata["public_market_adapters"]["summary"]["surface_inventory"]
        self.assertEqual("FAKE:X", inventory[0]["sample_instruments"][0])


class AdapterCapabilityTests(unittest.TestCase):
    def test_existing_adapter_is_resolved_but_missing_depth_remains_gap(self) -> None:
        daily = {
            "title": "TWSE public daily price adapter",
            "market_key": "global_discovery|Taiwan Stock Exchange",
            "spec": {
                "candidate": {
                    "venue_or_source": "Taiwan Stock Exchange",
                    "public_docs_url": "https://openapi.twse.com.tw/",
                    "why_interesting": "daily price coverage",
                }
            },
        }
        depth = copy.deepcopy(daily)
        depth["title"] = "TWSE order book depth adapter"
        depth["spec"]["candidate"]["why_interesting"] = "order book depth"
        self.assertEqual("fully_covered", adapter_capabilities.match_adapter_spec(daily)["match_status"])
        gap = adapter_capabilities.match_adapter_spec(depth)
        self.assertEqual("partial_capability_gap", gap["match_status"])
        self.assertIn("order_book", gap["missing_capabilities"])

        realtime = copy.deepcopy(daily)
        realtime["title"] = "TWSE real-time five-second market snapshot"
        realtime["spec"]["candidate"]["why_interesting"] = "intraday entry-quality price coverage"
        gap = adapter_capabilities.match_adapter_spec(realtime)
        self.assertEqual("partial_capability_gap", gap["match_status"])
        self.assertIn("entry_quality_quote", gap["missing_capabilities"])

    def test_reconciliation_closes_covered_spec_and_supersedes_duplicate(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        candidate = {
            "venue_or_source": "Taiwan Stock Exchange",
            "public_docs_url": "https://openapi.twse.com.tw/",
            "why_interesting": "daily prices",
        }
        for idx in range(2):
            storage.add_adapter_spec(
                conn,
                f"rec-{idx}",
                "global_discovery|Taiwan Stock Exchange",
                90 - idx,
                f"TWSE public daily price adapter {idx}",
                {"candidate": candidate},
                {},
            )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_capabilities, "REPORT_JSON", pathlib.Path(tmp) / "inventory.json"
        ), mock.patch.object(adapter_capabilities, "REPORT_MD", pathlib.Path(tmp) / "inventory.md"):
            report = adapter_capabilities.reconcile_adapter_specs(conn)
        statuses = [row["status"] for row in conn.execute("select status from adapter_specs order by id")]
        self.assertEqual("resolved_existing_adapter_capability", statuses[0])
        self.assertEqual("superseded_duplicate_adapter_spec", statuses[1])
        self.assertEqual(2, report["summary"]["specs_reconciled"])


class AdapterImplementationOwnerTests(unittest.TestCase):
    def test_owner_does_not_consume_attempt_marker_before_queue_write(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        storage.add_adapter_spec(
            conn,
            "tradable-lock",
            "global_discovery|Lock Test Exchange",
            90,
            "Lock test public prices",
            {
                "candidate": {
                    "venue_or_source": "Lock Test Exchange",
                    "data_access_type": "public_no_key",
                    "tradability_guess": "directly_tradable",
                    "confidence": 0.9,
                    "public_docs_url": "https://example.test/lock",
                    "source_validation_status": "public_url_present",
                }
            },
            {},
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_implementation_owner, "REPORT_JSON", pathlib.Path(tmp) / "owner.json"
        ), mock.patch.object(
            adapter_implementation_owner, "REPORT_MD", pathlib.Path(tmp) / "owner.md"
        ), mock.patch.object(
            adapter_implementation_owner, "MARKER", pathlib.Path(tmp) / "owner.marker"
        ) as marker, mock.patch.object(
            adapter_implementation_owner,
            "_update_spec_status",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                adapter_implementation_owner.run_once(
                    conn,
                    {"adapter_implementation_owner": {"enabled": True, "min_minutes_between_attempts": 0}},
                )
            self.assertFalse(marker.exists())
        conn.close()

    def test_owner_turns_best_tradable_spec_into_concrete_plugin_proposal(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        storage.add_adapter_spec(
            conn,
            "watch-only",
            "global_discovery|Watch Market",
            99,
            "Watch-only auction surface",
            {
                "candidate": {
                    "venue_or_source": "Watch Market",
                    "data_access_type": "public_no_key",
                    "tradability_guess": "watch_only",
                    "confidence": 0.99,
                    "public_docs_url": "https://example.test/watch",
                    "source_validation_status": "public_url_present",
                }
            },
            {},
        )
        storage.add_adapter_spec(
            conn,
            "tradable",
            "global_discovery|Nairobi Coffee Exchange",
            90,
            "Nairobi Coffee Exchange public prices",
            {
                "candidate": {
                    "candidate_id": "coffee-1",
                    "venue_or_source": "Nairobi Coffee Exchange",
                    "asset_or_event": "coffee auction settlement prices",
                    "data_access_type": "public_no_key",
                    "tradability_guess": "directly_tradable",
                    "confidence": 0.91,
                    "public_docs_url": "https://example.test/coffee",
                    "source_validation_status": "public_url_present",
                }
            },
            {},
        )
        captured = {}

        def fake_process(_conn, recommendation, _settings):
            captured.update(recommendation)
            return [{"proposal_id": "adapter-code-1", "action_status": "created", "status": "promoted"}]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_implementation_owner, "REPORT_JSON", pathlib.Path(tmp) / "owner.json"
        ), mock.patch.object(
            adapter_implementation_owner, "REPORT_MD", pathlib.Path(tmp) / "owner.md"
        ), mock.patch.object(
            adapter_implementation_owner, "MARKER", pathlib.Path(tmp) / "owner.marker"
        ), mock.patch.object(
            adapter_implementation_owner, "process_code_change_recommendation", side_effect=fake_process
        ):
            report = adapter_implementation_owner.run_once(
                conn,
                {"adapter_implementation_owner": {"enabled": True, "min_minutes_between_attempts": 0}},
            )

        self.assertEqual("deployed_waiting_acceptance", report["status"])
        self.assertEqual("adapter-spec:2:implementation", captured["recommendation_id"])
        payload = captured["payload"]
        self.assertEqual("public_data_adapter", payload["code_change"]["change_category"])
        self.assertIn(
            "src/adapters/venues/nairobi_coffee_exchange.py",
            payload["code_change"]["expected_files"],
        )
        status = conn.execute("select status from adapter_specs where id = 2").fetchone()["status"]
        self.assertEqual("deployed_waiting_acceptance", status)
        conn.close()

    def test_reconciliation_reopens_resolved_spec_when_capability_is_not_deployed(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        storage.add_adapter_spec(
            conn,
            "rec-realtime",
            "global_discovery|Taiwan Stock Exchange",
            92,
            "TWSE real-time five-second market snapshot",
            {
                "candidate": {
                    "venue_or_source": "Taiwan Stock Exchange",
                    "public_docs_url": "https://openapi.twse.com.tw/",
                    "why_interesting": "intraday entry-quality price coverage",
                }
            },
            {},
        )
        conn.execute("update adapter_specs set status = 'resolved_existing_adapter_capability'")
        conn.commit()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_capabilities, "REPORT_JSON", pathlib.Path(tmp) / "inventory.json"
        ), mock.patch.object(adapter_capabilities, "REPORT_MD", pathlib.Path(tmp) / "inventory.md"):
            adapter_capabilities.reconcile_adapter_specs(conn)
        row = conn.execute("select status, evidence_json from adapter_specs").fetchone()
        self.assertEqual("adapter_capability_gap", row["status"])
        evidence = json.loads(row["evidence_json"])
        self.assertIn(
            "entry_quality_quote",
            evidence["adapter_capability_reconciliation"]["missing_capabilities"],
        )
        conn.close()

    def test_auto_coder_accepts_new_adapter_plugin_as_runtime_integration(self) -> None:
        payload = {
            "action": "propose_code_change",
            "priority": 90,
            "title": "Add a public example exchange adapter",
            "rationale": "A sourced global discovery has public no-key market data.",
            "evidence": {"source_url": "https://example.test/docs"},
            "change_category": "public_data_adapter",
            "implementation_mode": "runtime_active",
            "expected_files": [
                "src/adapters/venues/example_exchange.py",
                "tests/test_public_market_adapters.py",
            ],
            "tests_to_run": ["python -m unittest tests.test_public_market_adapters"],
            "proposed_change": "Add and register a normalized ScanBatch adapter with parser tests.",
        }
        preflight = code_evolution.preflight_proposal(payload, DEFAULT_SETTINGS, root=ROOT)
        self.assertIn("src/adapters/venues/example_exchange.py", preflight["target_files"])
        self.assertEqual("integrated", preflight["quality_scorecard"]["runtime_integration_status"])
        self.assertFalse(preflight["quality_scorecard"]["reject_before_model_call"])


if __name__ == "__main__":
    unittest.main()
