from __future__ import annotations

import copy
import json
import pathlib
import ssl
import sqlite3
import sys
import tempfile
import types
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
from adapters.venues.b3 import (
    HUB_URL as B3_HUB_URL,
    B3PublicDataHubAdapter,
    parse_b3_public_data_hub,
)
from adapters.venues.bahrain_cross_listings import cross_listing_observations
from adapters.venues.bursa_derivatives import contract_observations
from adapters.venues.dc_department_of_energy_environment import (
    FINAL_SALES_URL as DC_SRC_FINAL_SALES_URL,
    FOR_SALE_URL as DC_SRC_FOR_SALE_URL,
    PROGRAM_RESOURCES_URL as DC_SRC_PROGRAM_RESOURCES_URL,
    DcDepartmentOfEnergyEnvironmentAdapter,
    parse_dc_src_final_sales,
    parse_dc_src_for_sale,
)
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
from adapters.venues.ethiopian_securities_exchange import (
    LISTED_COMPANIES_URL as ESX_LISTED_COMPANIES_URL,
    EthiopianSecuritiesExchangeAdapter,
    parse_esx_equity_listings,
)
from adapters.venues.kase_futures import parse_kase_futures
from adapters.venues.kalshi import (
    DOCS_URL as KALSHI_DOCS_URL,
    KalshiPublicPredictionMarketsAdapter,
    market_order_book_url as kalshi_order_book_url,
    markets_url as kalshi_markets_url,
    parse_kalshi_markets,
    parse_kalshi_order_book,
)
from adapters.venues.norwegian_block_exchange_nbx import (
    DOCS_URL as NBX_DOCS_URL,
    NorwegianBlockExchangeNbxAdapter,
    market_order_book_url,
    parse_nbx_order_book,
    parse_nbx_markets,
)
from adapters.venues.nzx_dairy import parse_nzx_gdt
from adapters.venues.polymarket import (
    DOCS_URL as POLYMARKET_SPORTS_DOCS_URL,
    SPORTS_WS_URL as POLYMARKET_SPORTS_WS_URL,
    PolymarketSportsWebSocketAdapter,
    fetch_sports_messages,
    parse_polymarket_sports_message,
)
from adapters.venues.stock_exchange_of_thailand_yuanta_securities_thailand import (
    SOURCE_URL as SET_YUANTA_SOURCE_URL,
    StockExchangeOfThailandYuantaSecuritiesThailandAdapter,
    parse_set_yuanta_dr_announcement,
)
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
            "ethiopian_securities_exchange",
            "norwegian_block_exchange_nbx_public",
            "kalshi_public_prediction_markets",
            "polymarket_sports_websocket",
            "b3_public_data_hub",
            "stock_exchange_of_thailand_yuanta_securities_thailand",
            "republican_stock_exchange_toshkent_public",
            "dc_department_of_energy_environment",
        }
        self.assertTrue(expected <= set(discover_adapters()))

    def test_dc_doee_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "dc_department_of_energy_environment"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("DC_DOEE", adapter.info.venue)
        self.assertEqual(DC_SRC_PROGRAM_RESOURCES_URL, adapter.info.docs_url)
        self.assertIn("event_price_reference", adapter.info.capabilities)
        self.assertIn("watershed", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_dc_doee_parsers_normalize_public_sale_and_listing_reports(self) -> None:
        final_sales_xml = """
        <qdbapi><errcode>0</errcode><errtext>No error</errtext><table>
          <fields>
            <field id="15"><label>Transfer Date</label></field>
            <field id="51"><label>Watershed where SRCs are generated</label></field>
            <field id="55"><label>Sewershed where SRCs are generated</label></field>
            <field id="21"><label>Number of SRCs</label></field>
            <field id="16"><label>Purchase price per SRC</label></field>
            <field id="32"><label>Value of transfer (paid by buyer)</label></field>
            <field id="90"><label>Transferred SRCs - BMP - Installation Date</label></field>
            <field id="89"><label>Transferred SRCs - Type of Activity</label></field>
            <field id="19"><label>Start serial number</label></field>
            <field id="26"><label>End serial number</label></field>
          </fields>
          <records><record rid="734">
            <f id="15">1785196800000</f><f id="51">Anacostia</f><f id="55">MS4</f>
            <f id="21">12,500</f><f id="16">1.82</f><f id="32">22750</f>
            <f id="90">1640995200000</f><f id="89">Voluntary</f>
            <f id="19">SRC-2022-0001</f><f id="26">SRC-2022-12500</f>
          </record></records>
        </table></qdbapi>
        """
        for_sale_xml = """
        <qdbapi><errcode>0</errcode><errtext>No error</errtext><table>
          <fields>
            <field id="10"><label>Number of SRCs for sale</label></field>
            <field id="21"><label>Buyer's price</label></field>
            <field id="100"><label>SRC Type</label></field>
            <field id="16"><label>SRC Watershed</label></field>
          </fields>
          <records><record rid="91">
            <f id="10">321884</f><f id="21">2.03</f><f id="100">High-Impact</f>
            <f id="16">Anacostia, Rock Creek</f>
          </record></records>
        </table></qdbapi>
        """

        sales = parse_dc_src_final_sales(
            final_sales_xml,
            received_at="2026-08-04T12:00:00+00:00",
        )
        listings = parse_dc_src_for_sale(
            for_sale_xml,
            received_at="2026-08-04T12:00:00+00:00",
        )

        sale = sales[0]
        self.assertEqual("DC_DOEE:SRC_SALE:734", sale["inst_id"])
        self.assertEqual(1.82, sale["last"])
        self.assertEqual(12500.0, sale["quantity_src"])
        self.assertEqual("Anacostia", sale["watershed"])
        self.assertEqual("MS4", sale["sewershed"])
        self.assertEqual("2026-07-28", sale["transfer_date"])
        self.assertEqual("fresh", sale["freshness_state"])
        self.assertEqual("closed", sale["session_status"])
        self.assertEqual(DC_SRC_FINAL_SALES_URL, sale["source_url"])
        self.assertEqual("watch_only", sale["direction"])

        listing = listings[0]
        self.assertEqual("DC_DOEE:SRC_FOR_SALE:91", listing["inst_id"])
        self.assertEqual(2.03, listing["buyer_price_per_src"])
        self.assertEqual(321884.0, listing["quantity_src_for_sale"])
        self.assertEqual(["Anacostia", "Rock Creek"], listing["watersheds"])
        self.assertEqual("seller_listing_active", listing["session_status"])
        self.assertEqual(DC_SRC_FOR_SALE_URL, listing["source_url"])
        self.assertEqual("watch_only", listing["direction"])

    def test_dc_doee_adapter_preserves_parser_and_fetch_evidence(self) -> None:
        reachable_bad_schema = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<qdbapi><errcode>0</errcode><table /></qdbapi>",
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
        with mock.patch(
            "adapters.venues.dc_department_of_energy_environment.fetch_text",
            side_effect=[reachable_bad_schema, blocked],
        ) as fetch:
            batch = DcDepartmentOfEnergyEnvironmentAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["final_sales"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["for_sale"]["fetch_status"])
        self.assertIn("required report fields", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertFalse(batch.metadata["contact_fields_requested"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))
        requested_urls = [call.args[0] for call in fetch.call_args_list]
        self.assertIn("clist=10.21.100.16", requested_urls[1])
        self.assertTrue(all("token" not in url.lower() for url in requested_urls))

    def test_esx_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "ethiopian_securities_exchange"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("ESX", adapter.info.venue)
        self.assertEqual(ESX_LISTED_COMPANIES_URL, adapter.info.docs_url)
        self.assertIn("equity_listing_catalog", adapter.info.capabilities)
        self.assertNotIn("ticker", adapter.info.capabilities)

    def test_esx_parser_normalizes_specified_official_equity_listings(self) -> None:
        document = """
        <html><body><table>
          <thead><tr>
            <th>Name</th><th>Symbol</th><th>Sector</th>
            <th>Date Listed</th><th>Date Incorporated</th>
          </tr></thead>
          <tbody>
            <tr><td><a href="/directory/bank-of-abyssinia-share-company/">Bank of Abyssinia Share Company</a></td><td>BOAX</td><td>Financial Services</td><td>2026-07-28</td><td>1996-02-16</td></tr>
            <tr><td>Abay Bank Share Company</td><td>ABAYB</td><td>Financial Services</td><td>2026-06-25</td><td>2010-07-14</td></tr>
            <tr><td>Ethio Telecom Share Company</td><td>TELE</td><td>Telecom Servicess</td><td>2026-05-26</td><td>2024-07-01</td></tr>
            <tr><td>Awash Bank Share Company</td><td>AWAB</td><td>Financial Services</td><td>2026-04-23</td><td>1994-11-10</td></tr>
            <tr><td>Older Issuer</td><td>OLDX</td><td>Other</td><td>2025-01-10</td><td>1997-06-11</td></tr>
          </tbody>
        </table></body></html>
        """
        rows = parse_esx_equity_listings(
            document,
            received_at="2026-08-04T12:00:00+00:00",
        )

        self.assertEqual(["BOAX", "ABAYB", "TELE", "AWAB"], [row["symbol"] for row in rows])
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual("ESX:EQUITY:BOAX", by_symbol["BOAX"]["inst_id"])
        self.assertEqual("2026-07-28", by_symbol["BOAX"]["listed_date"])
        self.assertEqual("Financial Services", by_symbol["ABAYB"]["sector"])
        self.assertEqual("fresh", by_symbol["BOAX"]["freshness_state"])
        self.assertEqual("stale", by_symbol["TELE"]["freshness_state"])
        self.assertEqual("listed", by_symbol["AWAB"]["session_status"])
        self.assertEqual("reachable", by_symbol["AWAB"]["fetch_status"])
        self.assertEqual(ESX_LISTED_COMPANIES_URL, by_symbol["AWAB"]["source_url"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["last"] == 0.0 for row in rows))

        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 3.0,
        }
        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            return_value=result,
        ):
            batch = EthiopianSecuritiesExchangeAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual(4, len(batch.observations))
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual(["fresh", "stale"], batch.metadata["freshness_states"])
        self.assertEqual("listed", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])

    def test_esx_adapter_preserves_parser_and_unavailable_fetch_evidence(self) -> None:
        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>an unrelated ESX page</body></html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:01:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }

        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            return_value=reachable,
        ):
            parser_batch = EthiopianSecuritiesExchangeAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable",
            parser_batch.metadata["fetch_status"]["listed_companies"]["fetch_status"],
        )
        self.assertIn("required headers", parser_batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("public_listing_parser_failure", parser_batch.observations[0]["candidate_reject_reason"])
        self.assertTrue(parser_batch.observations[0]["parser_failure"])

        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = EthiopianSecuritiesExchangeAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual(ESX_LISTED_COMPANIES_URL, unavailable_batch.observations[0]["source_url"])

    def test_set_yuanta_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "stock_exchange_of_thailand_yuanta_securities_thailand"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("SET", adapter.info.venue)
        self.assertEqual(SET_YUANTA_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("underlying_identity", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_set_yuanta_parser_normalizes_all_official_dr_references(self) -> None:
        announcement = """
        <html><body>
          <script>record={date:"2026-07-06T18:23:00+07:00",symbol:setSymbol}</script>
          <h1>11 New DRs issued by Yuanta</h1>
          <p>11 new depositary receipts issued by Yuanta Securities (Thailand).</p>
          <p>Trading will commence on July 7, 2026.</p>
          <ul>
            <li>"BABA19" on shares of Alibaba Group Holding Limited (9988)</li>
            <li>"SHINCHEM19" on shares of Shin-Etsu Chemical Co., Ltd. (4063)</li>
            <li>"AMAT19" on shares of Applied Materials, Inc. (AMAT)</li>
            <li>"AMZN19" on shares of Amazon.com, Inc. (AMZN)</li>
            <li>"GOOGL19" on shares of Alphabet Inc. Class A (GOOGL)</li>
            <li>"INTEL19" on shares of Intel Corporation (INTC)</li>
            <li>"KLAC19" on shares of KLA Corporation (KLAC)</li>
            <li>"LRCX19" on shares of Lam Research Corporation (LRCX)</li>
            <li>"PANW19" on shares of Palo Alto Networks, Inc. (PANW)</li>
            <li>"CAT19" on shares of Caterpillar Inc. (CAT)</li>
            <li>"DEAM19" on Invesco MDAX UCITS ETF Acc (DEAM)</li>
          </ul>
        </body></html>
        """
        rows = parse_set_yuanta_dr_announcement(
            announcement,
            received_at="2026-07-07T03:00:00+00:00",
        )

        self.assertEqual(11, len(rows))
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual("9988", by_symbol["BABA19"]["underlying_symbol"])
        self.assertEqual("HKEX", by_symbol["BABA19"]["underlying_venue"])
        self.assertEqual("INTC", by_symbol["INTEL19"]["underlying_symbol"])
        self.assertEqual("DEUTSCHE_BOERSE", by_symbol["DEAM19"]["underlying_venue"])
        self.assertEqual("listed", by_symbol["AMZN19"]["session_status"])
        self.assertEqual("fresh", by_symbol["AMZN19"]["freshness_state"])
        self.assertEqual("reachable", by_symbol["AMZN19"]["fetch_status"])
        self.assertEqual(SET_YUANTA_SOURCE_URL, by_symbol["AMZN19"]["source_url"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["last"] == 0.0 for row in rows))

        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": announcement,
            "received_at": "2026-07-07T03:00:00+00:00",
            "latency_ms": 3.0,
        }
        with mock.patch(
            "adapters.venues.stock_exchange_of_thailand_yuanta_securities_thailand.fetch_text",
            return_value=result,
        ):
            batch = StockExchangeOfThailandYuantaSecuritiesThailandAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual(11, len(batch.observations))
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("listed", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])

    def test_set_yuanta_adapter_preserves_reachable_parser_failure(self) -> None:
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html>an unrelated SET page</html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.stock_exchange_of_thailand_yuanta_securities_thailand.fetch_text",
            return_value=result,
        ):
            batch = StockExchangeOfThailandYuantaSecuritiesThailandAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(
            "reachable", batch.metadata["fetch_status"]["announcement"]["fetch_status"]
        )
        self.assertEqual("unknown", batch.metadata["freshness_state"])
        self.assertEqual("unknown", batch.metadata["session_state"])
        self.assertIn("marker", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual(
            "public_listing_parser_failure",
            batch.observations[0]["candidate_reject_reason"],
        )
        self.assertTrue(batch.observations[0]["parser_failure"])

    def test_set_yuanta_adapter_emits_watch_only_fetch_evidence_when_unavailable(self) -> None:
        result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:01:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.stock_exchange_of_thailand_yuanta_securities_thailand.fetch_text",
            return_value=result,
        ):
            batch = StockExchangeOfThailandYuantaSecuritiesThailandAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["announcement"]["fetch_status"])
        self.assertEqual("unknown", batch.metadata["freshness_state"])
        self.assertEqual("unknown", batch.metadata["session_state"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual("blocked", batch.observations[0]["fetch_status"])
        self.assertEqual(SET_YUANTA_SOURCE_URL, batch.observations[0]["source_url"])

    def test_b3_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        self.assertIn("b3_public_data_hub", discover_adapters())
        adapter = get_adapter("b3_public_data_hub")
        self.assertIsNotNone(adapter)
        self.assertEqual("B3", adapter.info.venue)
        self.assertEqual(B3_HUB_URL, adapter.info.docs_url)
        self.assertIn("unsponsored_bdr", adapter.info.capabilities)
        self.assertIn("cbio", adapter.info.capabilities)

    def test_b3_hub_parser_normalizes_all_documented_surfaces(self) -> None:
        html = """
        <section><h3>ESG products and services</h3>
          <a href="/en_us/b3/esg/otc-market.htm"><span>Decarbonization credits CBIO</span></a>
        </section>
        <section><h3>Investment funds</h3>
          <a href="/pt_br/fi-infra-listados/">FI-Infra</a>
          <a href="/en_us/fiagro.htm">FIAGRO</a>
          <a href="/pt_br/fidc.htm">FIDC</a>
        </section>
        <section><h3>BDRs e ETFs</h3>
          <a href="/en_us/stock-etf.htm">Stock Exchange Traded Fund - Stock ETF</a>
          <a href="/pt_br/bdr-etf.htm">BDRs ETFs</a>
          <a href="/en_us/unsponsored-bdr.htm">
            Unsponsored Brazilian Depositary Receipts - BDRs
          </a>
        </section>
        """
        rows = parse_b3_public_data_hub(
            html,
            received_at="2026-08-04T12:00:00+00:00",
        )

        self.assertEqual(7, len(rows))
        self.assertEqual(
            {
                "b3_unsponsored_bdr_public_data",
                "b3_bdr_etf_public_data",
                "b3_stock_etf_public_data",
                "b3_fi_infra_public_data",
                "b3_fiagro_public_data",
                "b3_fidc_public_data",
                "b3_cbio_public_data",
            },
            {row["market_surface"] for row in rows},
        )
        cbio = next(row for row in rows if row["symbol"] == "CBIO")
        self.assertEqual("https://www.b3.com.br/en_us/b3/esg/otc-market.htm", cbio["source_url"])
        self.assertEqual(B3_HUB_URL, cbio["source_catalog_url"])
        self.assertEqual("reachable", cbio["fetch_status"])
        self.assertEqual("fresh", cbio["freshness_state"])
        self.assertEqual("reference_catalog", cbio["session_status"])
        self.assertEqual("watch_only", cbio["direction"])
        self.assertEqual(0.0, cbio["last"])

    def test_b3_adapter_preserves_reachable_parser_failure(self) -> None:
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><a href='/only-one'>FIAGRO</a></html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch("adapters.venues.b3.fetch_text", return_value=result):
            batch = B3PublicDataHubAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["hub"]["fetch_status"])
        self.assertIn("required B3 public-data surfaces", batch.metadata["parser_failures"][0]["error"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual("public_catalog_parser_failure", batch.observations[0]["candidate_reject_reason"])
        self.assertIn("required B3 public-data surfaces", batch.observations[0]["parser_failure"])

    def test_b3_adapter_emits_watch_only_fetch_evidence_when_unavailable(self) -> None:
        result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch("adapters.venues.b3.fetch_text", return_value=result):
            batch = B3PublicDataHubAdapter().scan({})

        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["hub"]["fetch_status"])
        self.assertEqual("unknown", batch.metadata["freshness_state"])
        self.assertEqual("unknown", batch.metadata["session_state"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual("public_catalog_source_unavailable", batch.observations[0]["candidate_reject_reason"])
        self.assertEqual(B3_HUB_URL, batch.observations[0]["source_url"])

    def test_polymarket_sports_plugin_is_runtime_discoverable_and_watch_only(self) -> None:
        self.assertIn("polymarket_sports_websocket", discover_adapters())
        adapter = get_adapter("polymarket_sports_websocket")
        self.assertIsNotNone(adapter)
        self.assertEqual("POLYMARKET", adapter.info.venue)
        self.assertIn("live_score", adapter.info.capabilities)
        self.assertIn("websocket", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)
        self.assertEqual(POLYMARKET_SPORTS_DOCS_URL, adapter.info.docs_url)

    def test_polymarket_sports_parser_normalizes_live_game_state(self) -> None:
        row = parse_polymarket_sports_message(
            {
                "gameId": 5127839,
                "sportradarGameId": "sr:match:5127839",
                "slug": "lal-bos-2026-08-04",
                "leagueAbbreviation": "NBA",
                "homeTeam": "Los Angeles Lakers",
                "awayTeam": "Boston Celtics",
                "status": "InProgress",
                "live": True,
                "ended": False,
                "score": "98-94",
                "period": "Q4",
                "elapsed": "05:12",
            },
            received_at="2026-08-04T12:00:00+00:00",
        )

        self.assertEqual("POLYMARKET:SPORTS:NBA:5127839", row["inst_id"])
        self.assertEqual(98, row["home_score"])
        self.assertEqual(94, row["away_score"])
        self.assertEqual(98.0, row["last"])
        self.assertEqual("basketball", row["sport_family"])
        self.assertEqual("live", row["session_status"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(POLYMARKET_SPORTS_WS_URL, row["source_url"])

    def test_polymarket_sports_parser_accepts_sdk_envelope_and_source_freshness(self) -> None:
        row = parse_polymarket_sports_message(
            {
                "topic": "sports",
                "type": "sport_result",
                "payload": {
                    "game_id": 88,
                    "league_abbreviation": "NFL",
                    "home_team": "New York Jets",
                    "away_team": "Buffalo Bills",
                    "status": "Final",
                    "live": False,
                    "ended": True,
                    "score": "17-24",
                    "period": "FT",
                    "last_update": "2026-08-04T11:58:00Z",
                },
            },
            received_at="2026-08-04T12:00:00+00:00",
            stale_after_seconds=30,
        )

        self.assertEqual("american_football", row["sport_family"])
        self.assertEqual("ended", row["session_status"])
        self.assertEqual("stale", row["freshness_state"])
        self.assertEqual(120.0, row["freshness_age_seconds"])

    def test_polymarket_sports_parser_preserves_esports_composite_score(self) -> None:
        row = parse_polymarket_sports_message(
            {
                "gameId": 1596503,
                "leagueAbbreviation": "cs2",
                "homeTeam": "Team Alpha",
                "awayTeam": "Team Bravo",
                "status": "running",
                "live": True,
                "ended": False,
                "score": "12-11|1-0|Bo3",
                "period": "2/3",
            },
            received_at="2026-08-04T12:00:00+00:00",
        )

        self.assertEqual("esports", row["sport_family"])
        self.assertEqual(12, row["home_score"])
        self.assertEqual(11, row["away_score"])
        self.assertEqual(
            [{"home": 12, "away": 11}, {"home": 1, "away": 0}],
            row["score_components"],
        )
        self.assertEqual("Bo3", row["series_format"])

    def test_polymarket_sports_parser_covers_documented_sport_families(self) -> None:
        leagues = {
            "NFL": "american_football",
            "NHL": "ice_hockey",
            "MLB": "baseball",
            "NBA": "basketball",
            "CBB": "basketball",
            "CFB": "american_football",
            "EPL": "soccer",
            "CS2": "esports",
            "ATP": "tennis",
        }
        for game_id, (league, family) in enumerate(leagues.items(), start=1):
            with self.subTest(league=league):
                row = parse_polymarket_sports_message(
                    {
                        "gameId": game_id,
                        "leagueAbbreviation": league,
                        "status": "InProgress",
                        "live": True,
                        "ended": False,
                        "score": "1-0",
                    },
                    received_at="2026-08-04T12:00:00+00:00",
                )
                self.assertEqual(family, row["sport_family"])

    def test_polymarket_sports_transport_replies_to_heartbeat_without_subscription(self) -> None:
        class FakeTimeout(Exception):
            pass

        class FakeConnection:
            status = 101

            def __init__(self) -> None:
                self.frames = iter(
                    [
                        "ping",
                        json.dumps(
                            {
                                "gameId": 9,
                                "leagueAbbreviation": "MLB",
                                "status": "InProgress",
                                "live": True,
                                "ended": False,
                                "score": "3-2",
                            }
                        ),
                    ]
                )
                self.sent = []
                self.closed = False

            def settimeout(self, _timeout) -> None:
                return None

            def recv(self):
                try:
                    return next(self.frames)
                except StopIteration as exc:
                    raise FakeTimeout from exc

            def send(self, message) -> None:
                self.sent.append(message)

            def close(self) -> None:
                self.closed = True

        connection = FakeConnection()
        fake_websocket = types.SimpleNamespace(
            WebSocketTimeoutException=FakeTimeout,
            create_connection=mock.Mock(return_value=connection),
        )
        with mock.patch.dict(sys.modules, {"websocket": fake_websocket}):
            result = fetch_sports_messages(listen_seconds=1, max_messages=1)

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(result["messages"]))
        self.assertEqual(1, result["heartbeat_count"])
        self.assertEqual(["pong"], connection.sent)
        self.assertTrue(connection.closed)
        fake_websocket.create_connection.assert_called_once_with(
            POLYMARKET_SPORTS_WS_URL,
            timeout=8.0,
        )

    def test_polymarket_adapter_keeps_real_rows_and_parser_failure_evidence(self) -> None:
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 101,
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 8.0,
            "connection_state": "connected",
            "heartbeat_count": 1,
            "messages": [
                {
                    "gameId": 42,
                    "leagueAbbreviation": "NHL",
                    "homeTeam": "Rangers",
                    "awayTeam": "Bruins",
                    "status": "InProgress",
                    "live": True,
                    "ended": False,
                    "score": "2-1",
                    "period": "2Q",
                },
                {"unexpected": "schema"},
            ],
        }
        with mock.patch(
            "adapters.venues.polymarket.fetch_sports_messages",
            return_value=result,
        ):
            batch = PolymarketSportsWebSocketAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual(1, len(batch.observations))
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(1, batch.metadata["real_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["sports_stream"]["fetch_status"])
        self.assertIn("gameId or slug", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(batch.metadata["paper_only"])

    def test_polymarket_adapter_emits_watch_only_health_when_stream_is_unavailable(self) -> None:
        result = {
            "ok": False,
            "status": "unavailable",
            "http_status": None,
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 25.0,
            "connection_state": "connection_failed",
            "heartbeat_count": 0,
            "messages": [],
            "error": "connection refused",
        }
        with mock.patch(
            "adapters.venues.polymarket.fetch_sports_messages",
            return_value=result,
        ):
            batch = PolymarketSportsWebSocketAdapter().scan({})

        self.assertEqual("unavailable", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual(
            "public_sports_stream_unavailable",
            batch.observations[0]["candidate_reject_reason"],
        )
        self.assertEqual(POLYMARKET_SPORTS_WS_URL, batch.observations[0]["source_url"])

    def test_kalshi_plugin_is_runtime_discoverable_and_public_watch_only(self) -> None:
        self.assertIn("kalshi_public_prediction_markets", discover_adapters())
        adapter = get_adapter("kalshi_public_prediction_markets")
        self.assertIsNotNone(adapter)
        self.assertEqual("KALSHI", adapter.info.venue)
        self.assertIn("market_catalog", adapter.info.capabilities)
        self.assertIn("order_book", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)
        self.assertEqual(KALSHI_DOCS_URL, adapter.info.docs_url)

    def test_kalshi_market_parser_normalizes_open_binary_contract(self) -> None:
        source_url = kalshi_markets_url(10)
        rows = parse_kalshi_markets(
            {
                "markets": [
                    {
                        "ticker": "KXCPI-26AUG-T3.0",
                        "event_ticker": "KXCPI-26AUG",
                        "series_ticker": "KXCPI",
                        "title": "Will CPI inflation be above 3.0%?",
                        "category": "Economics",
                        "status": "open",
                        "yes_bid_dollars": "0.4200",
                        "yes_ask_dollars": "0.4600",
                        "no_bid_dollars": "0.5400",
                        "no_ask_dollars": "0.5800",
                        "last_price_dollars": "0.4400",
                        "volume_fp": "1234.00",
                        "volume_24h_fp": "321.00",
                        "open_interest_fp": "875.00",
                        "liquidity_dollars": "950.50",
                        "close_time": "2026-08-31T14:00:00Z",
                        "updated_time": "2026-08-04T11:59:00Z",
                    }
                ],
                "cursor": "next-page",
            },
            received_at="2026-08-04T12:00:00+00:00",
            source_url=source_url,
        )

        row = rows[0]
        self.assertEqual("KALSHI:KXCPI-26AUG-T3.0", row["inst_id"])
        self.assertEqual("prediction_market", row["market_type"])
        self.assertEqual("Economics", row["category"])
        self.assertEqual(0.44, row["last"])
        self.assertEqual(0.42, row["yes_bid"])
        self.assertEqual(0.46, row["yes_ask"])
        self.assertEqual(400.0, row["spread_bps_of_payout"])
        self.assertEqual(321.0, row["volume_24h_contracts"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("open", row["session_status"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(source_url, row["source_url"])

    def test_kalshi_order_book_parser_derives_implied_asks_and_depth(self) -> None:
        market = parse_kalshi_markets(
            {
                "markets": [
                    {
                        "ticker": "KXTECH-26DEC-TYES",
                        "title": "Will a technology milestone occur?",
                        "yes_bid_dollars": "0.4100",
                        "yes_ask_dollars": "0.4700",
                        "last_price_dollars": "0.4400",
                    }
                ]
            },
            received_at="2026-08-04T12:00:00+00:00",
        )[0]
        source_url = kalshi_order_book_url(market["ticker"])
        row = parse_kalshi_order_book(
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.3000", "5.00"], ["0.4200", "10.00"], ["0.4200", "2.00"]],
                    "no_dollars": [["0.2000", "4.00"], ["0.5400", "8.00"]],
                }
            },
            market=market,
            received_at="2026-08-04T12:00:01+00:00",
            source_url=source_url,
        )

        self.assertEqual(0.42, row["yes_bid"])
        self.assertEqual(0.46, row["yes_ask"])
        self.assertEqual(0.44, row["last"])
        self.assertEqual([0.42, 12.0], row["book_levels"]["yes_bids"][0])
        self.assertEqual(17.0, row["yes_depth_contracts"])
        self.assertEqual(12.0, row["no_depth_contracts"])
        self.assertEqual("official_order_book", row["quality_status"])
        self.assertEqual(source_url, row["source_url"])
        self.assertIn("status=open", row["contract_source_url"])
        self.assertEqual("watch_only", row["direction"])

    def test_kalshi_order_book_parser_accepts_schema_valid_empty_book(self) -> None:
        market = parse_kalshi_markets(
            {
                "markets": [
                    {
                        "ticker": "KXENTERTAINMENT-26-TYES",
                        "title": "Will the film win?",
                        "yes_bid_dollars": "0.20",
                        "yes_ask_dollars": "0.30",
                    }
                ]
            },
            received_at="2026-08-04T12:00:00+00:00",
        )[0]
        row = parse_kalshi_order_book(
            {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
            market=market,
            received_at="2026-08-04T12:00:01+00:00",
        )

        self.assertEqual("empty", row["order_book_state"])
        self.assertEqual("official_order_book_empty", row["quality_status"])
        self.assertEqual(0.25, row["last"])
        self.assertEqual(market["source_url"], row["source_url"])
        self.assertIn("/orderbook", row["order_book_source_url"])
        self.assertEqual("watch_only", row["direction"])

    def test_kalshi_adapter_preserves_book_parser_failure_and_paper_safety(self) -> None:
        catalog = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": json.dumps(
                {
                    "markets": [
                        {
                            "ticker": "KXCLIMATE-26-T1",
                            "title": "Will the climate threshold be crossed?",
                            "status": "open",
                            "yes_bid_dollars": "0.35",
                            "yes_ask_dollars": "0.40",
                            "volume_24h_fp": "200",
                        }
                    ]
                }
            ),
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 3.0,
        }
        malformed_book = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": '{"orderbook": {"yes": []}}',
            "received_at": "2026-08-04T12:00:01+00:00",
            "latency_ms": 2.0,
        }

        def fake_fetch(url, _timeout):
            return catalog if "?status=open" in url else malformed_book

        with mock.patch("adapters.venues.kalshi.fetch_text", side_effect=fake_fetch):
            batch = KalshiPublicPredictionMarketsAdapter().scan(
                {
                    "public_market_adapters": {
                        "kalshi_public_prediction_markets": {"market_limit": 10, "max_order_books": 1}
                    }
                }
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(1, batch.metadata["real_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["catalog"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["KXCLIMATE-26-T1"]["fetch_status"])
        self.assertIn("orderbook_fp", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(batch.metadata["paper_only"])
        row = batch.observations[0]
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual("public_order_book_parser_failure", row["candidate_reject_reason"])
        self.assertIn("orderbook_fp", row["parser_failure"])

    def test_kalshi_adapter_emits_watch_only_health_evidence_when_unreachable(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch("adapters.venues.kalshi.fetch_text", return_value=blocked):
            batch = KalshiPublicPredictionMarketsAdapter().scan({})

        self.assertEqual("blocked", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertEqual([], batch.candidates)
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        self.assertEqual("unknown", batch.observations[0]["freshness_state"])
        self.assertEqual("public_prediction_market_source_unavailable", batch.observations[0]["candidate_reject_reason"])
        self.assertIn("status=open", batch.observations[0]["source_url"])

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
    def test_dc_doee_adapter_closes_src_registry_price_spec(self) -> None:
        spec = {
            "title": "Implement public adapter #1254: DC DOEE SRC sale prices",
            "market_key": "global_discovery|DC Department of Energy & Environment",
            "spec": {
                "candidate": {
                    "venue_or_source": "DC Department of Energy & Environment",
                    "public_docs_url": DC_SRC_PROGRAM_RESOURCES_URL,
                    "asset_or_event": "SRC sale prices, quantity, watershed, and sewershed",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("dc_department_of_energy_environment", match["adapter_id"])
        self.assertIn("event_price_reference", match["available_capabilities"])

    def test_esx_adapter_closes_listing_spec_without_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #1295: Ethiopian Securities Exchange",
            "market_key": "global_discovery|Ethiopian Securities Exchange",
            "spec": {
                "candidate": {
                    "venue_or_source": "Ethiopian Securities Exchange",
                    "public_docs_url": ESX_LISTED_COMPANIES_URL,
                    "asset_or_event": "ESX equity listings BOAX ABAYB TELE AWAB",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("ethiopian_securities_exchange", match["adapter_id"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])
        self.assertNotIn("ticker", match["available_capabilities"])

    def test_b3_adapter_closes_public_surface_spec_without_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #622: B3",
            "market_key": "global_discovery|B3",
            "spec": {
                "candidate": {
                    "venue_or_source": "B3",
                    "public_docs_url": B3_HUB_URL,
                    "asset_or_event": (
                        "Unsponsored BDRs, BDR ETFs, stock ETFs, FI-Infra, "
                        "FIAGRO, FIDC, and CBIO"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("b3_public_data_hub", match["adapter_id"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

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
