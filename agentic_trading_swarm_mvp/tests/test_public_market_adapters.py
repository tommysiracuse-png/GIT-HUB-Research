from __future__ import annotations

import copy
import datetime as dt
import io
import json
import pathlib
import ssl
import sqlite3
import sys
import tempfile
import types
import unittest
import urllib.error
import zipfile
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
from adapters.venues.aib_eex_france import (
    EEX_CPB_PAGE_URL,
    SOURCE_URL as AIB_EEX_FRANCE_SOURCE_URL,
    AibEexFranceBiomethaneCpbAdapter,
    parse_aib_eex_france_cpb_workbook,
    parse_aib_eex_france_cpb_reporting,
)
from adapters.venues.australian_office_of_financial_management_aofm import (
    DATA_HUB_URL as AOFM_DATA_HUB_URL,
    FORTHCOMING_TRANSACTIONS_URL as AOFM_FORTHCOMING_TRANSACTIONS_URL,
    AustralianOfficeOfFinancialManagementAofmAdapter,
    parse_aofm_forthcoming_transactions,
    parse_aofm_treasury_bond_issuance_workbook,
)
from adapters.venues.bahrain_cross_listings import cross_listing_observations
from adapters.venues.bank_of_canada import (
    API_URL as BANK_OF_CANADA_TBILL_API_URL,
    SOURCE_URL as BANK_OF_CANADA_TBILL_SOURCE_URL,
    BankOfCanadaRegularTreasuryBillsAdapter,
    parse_bank_of_canada_treasury_bill_auctions,
)
from adapters.venues.bursa_derivatives import (
    SOURCE_URL as BURSA_DERIVATIVES_SOURCE_URL,
    BursaMalaysiaDerivativesBerhadAdapter,
    contract_observations,
    parse_bursa_derivatives_contract_catalog,
)
from adapters.venues.papua_new_guinea_customs_service import (
    SOURCE_URL as PNG_CUSTOMS_TSC_SOURCE_URL,
    PapuaNewGuineaCustomsServiceAdapter,
    parse_papua_new_guinea_customs_tscs,
)
from adapters.venues.nairobi_coffee_exchange import (
    SOURCE_URL as NAIROBI_COFFEE_EXCHANGE_SOURCE_URL,
    NairobiCoffeeExchangeAdapter,
    parse_nairobi_coffee_exchange_market_report,
)
from adapters.venues.casablanca_stock_exchange_futures_market import (
    SOURCE_URL as CASABLANCA_FUTURES_SOURCE_URL,
    CasablancaStockExchangeFuturesMarketAdapter,
    parse_casablanca_masi20_future,
)
from adapters.venues.california_air_resources_board import (
    AUCTION_INFORMATION_URL as CARB_AUCTION_INFORMATION_URL,
    DATA_DASHBOARD_FILES_URL as CARB_DATA_DASHBOARD_FILES_URL,
    MAY_2026_NOTICE_URL as CARB_MAY_2026_NOTICE_URL,
    RESULTS_SUMMARY_URL as CARB_RESULTS_SUMMARY_URL,
    CaliforniaAirResourcesBoardAdapter,
    parse_carb_auction_information,
    parse_carb_auction_notice,
    parse_carb_auction_results,
)
from adapters.venues.central_bank_of_bahrain import (
    RESULTS_URL as CBB_TBILL_RESULTS_URL,
    SOURCE_URL as CBB_TBILL_SOURCE_URL,
    CentralBankOfBahrainTreasuryBillsAdapter,
    parse_central_bank_of_bahrain_treasury_bill_auction,
)
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
    UK_ETS_PAGE_URL as EEX_UK_ETS_PAGE_URL,
    EexEuaPrimaryAuctionSpotAdapter,
    EexGermanNehsAdapter,
    EexUkaFuturesOptionsAdapter,
    datasource_auction_url,
    datasource_spot_url,
    parse_eex_emissions_spot,
    parse_eex_eu_ets_auction,
    parse_eex_eua_auction_workbook,
    parse_eex_nehs_auction,
    parse_eex_nehs_sales,
    parse_eex_uka_futures_options,
)
from adapters.venues.e_auksion_district_hokimiyat_notices import (
    API_URL as E_AUKSION_API_URL,
    NOTICE_URL as E_AUKSION_NOTICE_URL,
    EAuksionDistrictHokimiyatNoticesAdapter,
    parse_e_auksion_lots,
)
from adapters.venues.ethiopian_securities_exchange import (
    FIXED_INCOME_INSTRUMENTS_URL as ESX_FIXED_INCOME_INSTRUMENTS_URL,
    FIXED_INCOME_OPERATIONS_URL as ESX_FIXED_INCOME_OPERATIONS_URL,
    FIXED_INCOME_OVERVIEW_URL as ESX_FIXED_INCOME_OVERVIEW_URL,
    LISTED_COMPANIES_URL as ESX_LISTED_COMPANIES_URL,
    EthiopianSecuritiesExchangeAdapter,
    EthiopianSecuritiesExchangeFixedIncomeAdapter,
    parse_esx_equity_listings,
    parse_esx_fixed_income_instruments,
    parse_esx_fixed_income_session,
)
from adapters.venues.kase_futures import parse_kase_futures
from adapters.venues.kazakhstan_stock_exchange_kase import (
    MARKET_MAKER_NOTICE_URL as KASE_GLOBAL_DOCS_URL,
    KazakhstanStockExchangeKaseGlobalAdapter,
    parse_kase_global,
)
from adapters.venues.kalshi import (
    DOCS_URL as KALSHI_DOCS_URL,
    KalshiPublicPredictionMarketsAdapter,
    market_order_book_url as kalshi_order_book_url,
    markets_url as kalshi_markets_url,
    parse_kalshi_markets,
    parse_kalshi_order_book,
)
from adapters.venues.ministry_of_finance_uae_federal_debt_management_office import (
    SOURCE_URL as UAE_MOF_ISSUANCE_PROGRAMME_URL,
    MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter,
    parse_uae_federal_debt_issuance_programme,
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
from adapters.venues.suruhanjaya_tenaga_energy_commission import (
    SOURCE_URL as ENEGEM_SOURCE_URL,
    SuruhanjayaTenagaEnergyCommissionAdapter,
    parse_enegem_programme,
)
from adapters.venues.government_of_india_ministry_of_msme_pib import (
    SOURCE_URL as INDIA_TREDS_PIB_SOURCE_URL,
    GovernmentOfIndiaMinistryOfMsmePibAdapter,
    parse_india_treds_pib_release,
)
from adapters.venues.twse_daily import parse_twse_daily
from adapters.venues.vietnam_securities_depository_and_clearing_corporation_hanoi_sto import (
    GUIDELINE_URL as VSDC_CARBON_GUIDELINE_URL,
    HNX_COORDINATION_URL as VSDC_HNX_COORDINATION_URL,
    SETTLEMENT_URL as VSDC_CARBON_SETTLEMENT_URL,
    VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter,
    parse_hnx_carbon_coordination,
    parse_vsdc_carbon_guideline,
    parse_vsdc_carbon_settlement_rules,
)
from adapters.venues.hanoi_stock_exchange_state_securities_commission import (
    CARBON_PORTAL_URL as HNX_SSC_CARBON_PORTAL_URL,
    SSC_DECREE_URL as HNX_SSC_DECREE_URL,
    TRADING_NOTICE_URL as HNX_SSC_TRADING_NOTICE_URL,
    HanoiStockExchangeStateSecuritiesCommissionAdapter,
    parse_hnx_vn2025_trading_notice,
)
from scan_batch import ScanBatch
from settings import DEFAULT_SETTINGS


def _esx_fixed_income_instruments_fixture() -> str:
    return """
    <html><body>
      <h1>Treasury Bills and Bonds</h1>
      <ul>
        <li>Treasury Bills (T-Bills) are short-term debt securities issued by
        the Ethiopian government with a maturity period of one year or less.</li>
        <li>T-Bills are auctioned biweekly with a minimum investment amount of
        ETB 5,000 in maturities of 28-days, 91-days, 182-days and 364-days.</li>
        <li>Treasury Bonds are long-term government debt securities with a
        maturity period of more than one year.</li>
      </ul>
      <h1>Corporate Bonds</h1>
      <p>A corporate bond is issued by a company at fixed or variable interest.</p>
      <h1>REPURCHASE AGREEMENTS/ REPOS</h1>
      <p>A repurchase agreement (repo) is secured short-term borrowing, usually
      a 1-7 day term.</p>
      <h1>Commercial Papers</h1>
      <p>Commercial papers are corporate obligations with a maturity period of
      less than 270 days.</p>
    </body></html>
    """


def _esx_fixed_income_operations_fixture() -> str:
    return """
    <html><body>
      <table>
        <tr><th>Session</th><th>Time</th><th>Price Limit</th></tr>
        <tr><td>Pre-open</td><td>9:00 AM - 9:30 AM</td><td>-</td></tr>
        <tr><td>Continuous</td><td>9:30 AM - 3:00 PM</td><td>-</td></tr>
        <tr><td>Close</td><td>3:00 PM</td><td>-</td></tr>
      </table>
      <table>
        <tr><th>Date</th><th>Holiday</th></tr>
        <tr><td>Tuesday, Aug 25, 2026</td><td>The Prophet's Birthday</td></tr>
      </table>
    </body></html>
    """


def _eex_auction_workbook_fixture() -> bytes:
    headers = [
        "Date",
        "Time",
        "Auction Name",
        "Contract",
        "Status",
        "Auction Price €/tCO2",
        "Auction Volume tCO2",
        "Total Amount of Bids",
        "Cover Ratio",
        "Number of bids submitted",
        "Number of successful bids",
        "Total Number of Bidders",
        "Number of Successful Bidders",
        "Total Revenue €",
        "Zone",
    ]
    text_values = {
        3: "EEX EUAA Primary Auction Spot",
        4: "T3AA",
        5: "successful",
        15: "EU",
    }

    def column_name(index: int) -> str:
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    header_cells = "".join(
        f'<c r="{column_name(index)}1" t="inlineStr"><is><t>{value}</t></is></c>'
        for index, value in enumerate(headers, 1)
    )
    excel_date = (dt.date(2026, 8, 4) - dt.date(1899, 12, 30)).days
    numeric_values = {
        1: excel_date,
        2: 11 / 24,
        6: 80.86,
        7: 2_246_500,
        8: 4_013_000,
        9: 1.79,
        10: 116,
        11: 20,
        12: 22,
        13: 14,
        14: 181_651_990,
    }
    data_cells = "".join(
        f'<c r="{column_name(index)}2"><v>{value}</v></c>'
        for index, value in numeric_values.items()
    ) + "".join(
        f'<c r="{column_name(index)}2" t="inlineStr"><is><t>{value}</t></is></c>'
        for index, value in text_values.items()
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="1">{header_cells}</row><row r="2">{data_cells}</row></sheetData>'
        '</worksheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _eex_uka_contract_fixture() -> str:
    return """
    <html><body>
      <h1>UK ETS Futures and Options</h1>
      <h2>UKA Product Overview</h2>
      <h3>UKA Futures</h3>
      <table>
        <tr><th>Contracts</th><td>UKA Futures</td></tr>
        <tr><th>Delivery periods</th><td>First delivery starts from December 2026</td></tr>
        <tr><th>Contract volume</th><td>1,000 UKA</td></tr>
        <tr><th>Minimum tick</th><td>£0.01 per UKA</td></tr>
      </table>
      <h3>UKA Options</h3>
      <table>
        <tr><th>Contracts</th><td>UKA Options</td></tr>
        <tr><th>Underlying</th><td>The underlying is the EEX UKA Dec Futures.</td></tr>
        <tr><th>Option type</th><td>European</td></tr>
      </table>
    </body></html>
    """


def _aib_eex_france_cpb_protocol_fixture() -> str:
    return """
    Association of Issuing Bodies
    EECS DOMAIN PROTOCOL FOR EEX - FRANCE
    Date 21 January 2026
    IV. NATIONAL BIOGAS CERTIFICATES
    G. Activity Reporting
    G.1 Public reports
    EEX publishes on a monthly basis the list of CPBs which have been issued in
    the registry. Data includes: the date of commissioning of the installation;
    the quantity of biomethane, expressed in MWh, for which the certificate was
    issued; the start and end dates of the injection period for the batch of
    biomethane; and the date of issuance of the certificate. Furthermore, EEX
    publishes on a monthly basis the average price at which these certificates
    have been purchased or sold.
    """


def _aib_eex_france_cpb_workbook_fixture() -> bytes:
    def sheet(rows: list[list[str]]) -> str:
        cells = []
        for row_index, values in enumerate(rows, 1):
            row_cells = "".join(
                f'<c r="{chr(65 + column)}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
                for column, value in enumerate(values)
            )
            cells.append(f'<row r="{row_index}">{row_cells}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(cells)}</sheetData></worksheet>'
        )

    price_sheet = sheet(
        [
            ["Mois de publication", "Certificats 2025", "Certificats 2026"],
            ["Janvier 2026", "/", "83,03 €"],
            ["Février 2026", "/", "84,49 €"],
        ]
    )
    issuance_sheet = sheet(
        [
            [
                "Date d'émission",
                "Volume des certificats",
                "Unité",
                "Volume en MWh",
                "Date de début de production",
                "Date de fin de production",
                "Date de mise en service",
            ],
            ["46189", "7371", "CPB", "7371", "46113", "46142", "44916"],
        ]
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", price_sheet)
        archive.writestr("xl/worksheets/sheet2.xml", issuance_sheet)
    return output.getvalue()


def _aofm_tender_results_workbook_fixture() -> bytes:
    values = [
        [
            "Date Held",
            "Tender Number",
            "Maturity",
            "Coupon",
            "ISIN",
            "Amount Offered",
            "Amount Allotted",
            "Coverage Ratio",
            "Weighted Average Yield (%)",
            "Date Settled",
        ],
        [
            "5/08/2026",
            "TB2026-12",
            "21/03/2047",
            "3.00",
            "AU000XCLWAM8",
            "1000000000",
            "1000000000",
            "2.15",
            "4.321",
            "7/08/2026",
        ],
    ]
    cells = []
    for row_index, row in enumerate(values, 1):
        row_cells = "".join(
            f'<c r="{chr(65 + column)}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column, value in enumerate(row)
        )
        cells.append(f'<row r="{row_index}">{row_cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(cells)}</sheetData></worksheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


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
            "kazakhstan_stock_exchange_kase_global",
            "nzx_gdt_event_reference",
            "bursa_derivatives_contract_catalog",
            "bahrain_cross_listings_catalog",
            "eex_german_nehs_public",
            "eex_eua_primary_auction_spot_public",
            "eex_uka_futures_options_public",
            "e_auksion_district_hokimiyat_notices",
            "ethiopian_securities_exchange",
            "norwegian_block_exchange_nbx_public",
            "kalshi_public_prediction_markets",
            "polymarket_sports_websocket",
            "b3_public_data_hub",
            "bank_of_canada_regular_treasury_bills",
            "central_bank_of_bahrain_treasury_bills",
            "australian_office_of_financial_management_aofm",
            "stock_exchange_of_thailand_yuanta_securities_thailand",
            "republican_stock_exchange_toshkent_public",
            "dc_department_of_energy_environment",
            "vietnam_securities_depository_and_clearing_corporation_hanoi_sto",
            "abu_dhabi_securities_exchange_adx_etf",
        }
        self.assertTrue(expected <= set(discover_adapters()))

    def test_bank_of_canada_tbill_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "bank_of_canada_regular_treasury_bills"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("BANK_OF_CANADA", adapter.info.venue)
        self.assertEqual(BANK_OF_CANADA_TBILL_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("stop_out_yield", adapter.info.capabilities)
        self.assertIn("award_size", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_bursa_derivatives_parser_normalizes_published_contract_catalog(self) -> None:
        document = """
        <html><body>
          <h1>Rules of Bursa Malaysia Derivatives Berhad</h1>
          <h2>Schedule 1 Commodity Contracts</h2>
          <p>FCPO Crude Palm Oil Futures Contract</p>
          <h2>Schedule 2 Equity Contracts</h2>
          <p>FKLI FTSE Bursa Malaysia KLCI Futures Contract</p>
          <p>OKLI Option on FTSE Bursa Malaysia KLCI Futures</p>
          <p>FM70 Mini FTSE Bursa Malaysia Mid 70 Index Futures Contract</p>
          <p>F4GM FTSE4Good Bursa Malaysia Index Futures Contract</p>
          <p>FMG5 Mini Gold Futures Contract</p>
        </body></html>
        """
        rows = parse_bursa_derivatives_contract_catalog(
            document,
            received_at="2026-08-04T05:30:00+00:00",
        )

        self.assertEqual({"FCPO", "FKLI", "OKLI", "FM70", "F4GM", "FMG5"}, {row["symbol"] for row in rows})
        fcpo = next(row for row in rows if row["symbol"] == "FCPO")
        okli = next(row for row in rows if row["symbol"] == "OKLI")
        self.assertEqual("BURSA_MALAYSIA_DERIVATIVES:FCPO", fcpo["inst_id"])
        self.assertEqual("commodity_futures", fcpo["asset_class"])
        self.assertEqual("options_catalog", okli["market_type"])
        self.assertEqual("reference_only", fcpo["session_status"])
        self.assertEqual("fresh", fcpo["freshness_state"])
        self.assertEqual(BURSA_DERIVATIVES_SOURCE_URL, fcpo["source_url"])
        self.assertEqual("watch_only", fcpo["direction"])
        self.assertEqual(0.0, fcpo["last"])

    def test_bursa_derivatives_runtime_emits_catalog_observations_for_reachable_source(self) -> None:
        document = b"""
        Rules of Bursa Malaysia Derivatives Berhad
        FCPO FKLI OKLI FM70 F4GM FMG5
        """
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": document,
            "received_at": "2026-08-04T05:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch("adapters.venues.bursa_derivatives.fetch_bytes", return_value=result):
            batch = BursaMalaysiaDerivativesBerhadAdapter().scan({})

        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["rules"]["fetch_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("reference_only", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual(6, len(batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(all(row["last"] == 0.0 for row in batch.observations))

    def test_bursa_derivatives_plugin_is_discoverable_and_preserves_failures(self) -> None:
        adapter_id = "bursa_derivatives_contract_catalog"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, BursaMalaysiaDerivativesBerhadAdapter)
        self.assertEqual(BURSA_DERIVATIVES_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("settlement_reference", adapter.info.capabilities)

        reachable_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": b"Rules of Bursa Malaysia Derivatives Berhad replacement document",
            "received_at": "2026-08-04T05:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.bursa_derivatives.fetch_bytes", return_value=reachable_result
        ):
            parser_batch = BursaMalaysiaDerivativesBerhadAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["rules"]["fetch_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_bursa_derivatives_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable_result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "content": b"",
            "received_at": "2026-08-04T05:31:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.bursa_derivatives.fetch_bytes", return_value=unavailable_result
        ):
            unavailable_batch = BursaMalaysiaDerivativesBerhadAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_bursa_derivatives_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_png_customs_tsc_parser_normalizes_vehicle_classification_rows(self) -> None:
        document = """
        Papua New Guinea Customs Service
        Tariff Specification Codes (TSCs) List - Implementation for Motor Vehicles
        The use of the TSC list is mandatory from 1 June 2023.
        Toyota Hilux 1KD-FTV 3000cc 2005-2015 Tariff Specification Code: TSC-TOY-001
        Toyota Corolla 1NZ-FE 1500cc 2006-2012 Tariff Specification Code: TSC-TOY-002
        Nissan X-Trail QR25-DE 2500cc 2007-2014 Tariff Specification Code: TSC-NIS-010
        """
        rows = parse_papua_new_guinea_customs_tscs(
            document, received_at="2026-08-04T05:30:00+00:00"
        )

        self.assertEqual(3, len(rows))
        hilux = next(row for row in rows if row["tariff_specification_code"] == "TSC-TOY-001")
        self.assertEqual("PNG_CUSTOMS", hilux["venue"])
        self.assertEqual("Toyota", hilux["vehicle_make"])
        self.assertEqual("Hilux", hilux["vehicle_model"])
        self.assertEqual("1KD-FTV", hilux["engine_code"])
        self.assertEqual(3000, hilux["engine_capacity_cc"])
        self.assertEqual(2005, hilux["model_year_from"])
        self.assertEqual(2015, hilux["model_year_to"])
        self.assertEqual("2023-06-01", hilux["mandatory_from"])
        self.assertEqual("mandatory_in_force", hilux["session_status"])
        self.assertEqual(PNG_CUSTOMS_TSC_SOURCE_URL, hilux["source_url"])
        self.assertEqual("watch_only", hilux["direction"])
        self.assertEqual(0.0, hilux["last"])

    def test_png_customs_tsc_plugin_is_discoverable_and_preserves_source_failures(self) -> None:
        adapter_id = "papua_new_guinea_customs_service"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, PapuaNewGuineaCustomsServiceAdapter)
        self.assertEqual(PNG_CUSTOMS_TSC_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("vehicle_tariff_specification_code", adapter.info.capabilities)

        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": (
                "PNG Customs TSC List for Motor Vehicles. Mandatory from 1 June 2023. "
                "Toyota Vitz 1KR-FE 1000cc 2005-2010 Tariff Specification Code: TSC-TOY-100"
            ),
            "received_at": "2026-08-04T05:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.papua_new_guinea_customs_service.fetch_bytes", return_value=reachable
        ):
            batch = PapuaNewGuineaCustomsServiceAdapter().scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["tsc_list"]["fetch_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("mandatory_in_force", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual("TSC-TOY-100", batch.observations[0]["tariff_specification_code"])

        malformed = {**reachable, "text": "PNG Customs TSC List for Motor Vehicles"}
        with mock.patch(
            "adapters.venues.papua_new_guinea_customs_service.fetch_bytes", return_value=malformed
        ):
            parser_batch = PapuaNewGuineaCustomsServiceAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["tsc_list"]["fetch_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_png_customs_tsc_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "content": b"",
            "received_at": "2026-08-04T05:31:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.papua_new_guinea_customs_service.fetch_bytes", return_value=unavailable
        ):
            unavailable_batch = PapuaNewGuineaCustomsServiceAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_png_customs_tsc_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_png_customs_tsc_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        target_adapter_id = "papua_new_guinea_customs_service"
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": (
                "PNG Customs TSC List for Motor Vehicles. Mandatory from 1 June 2023. "
                "Toyota Aqua 1NZ-FXE 1500cc 2011-2015 Tariff Specification Code: TSC-TOY-200"
            ),
            "received_at": "2026-08-04T05:30:00+00:00",
            "latency_ms": 4.0,
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_png() -> list[str]:
            return [
                adapter_id
                for adapter_id in original_discover()
                if adapter_id == target_adapter_id
            ]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.papua_new_guinea_customs_service.fetch_bytes", return_value=result
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_png):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {target_adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(1, len(batch.observations))
        self.assertEqual("PNG_CUSTOMS", batch.observations[0]["venue"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(target_adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_nairobi_coffee_exchange_parser_normalizes_grade_market_totals(self) -> None:
        report = """
        Sale 25 of Wednesday, April 8, 2026
        MARKET TOTAL
        GRADE Bags offered Weight offered Min price Max price Value (USD) Average price
        AA 1,110 68,488 285.00 400.00 473,419.38 345.62
        AB 2,597 159,279 136.00 390.00 1,046,813.20 328.61
        UG1 1,196 73,721 100.00 308.00 377,476.28 256.02
        Note Prices are in USD per 50 Kg
        NAIROBI COFFEE EXCHANGE
        """
        rows = parse_nairobi_coffee_exchange_market_report(
            report, received_at="2026-04-10T12:00:00+00:00"
        )

        self.assertEqual({"AA", "AB", "UG1"}, {row["symbol"].removeprefix("NCE_") for row in rows})
        aa = next(row for row in rows if row["symbol"] == "NCE_AA")
        self.assertEqual("NAIROBI_COFFEE_EXCHANGE:SALE:25:GRADE:AA", aa["inst_id"])
        self.assertEqual(345.62, aa["last"])
        self.assertEqual(285.0, aa["published_min_price_usd_per_50kg"])
        self.assertEqual(400.0, aa["published_max_price_usd_per_50kg"])
        self.assertEqual(115.0, aa["grade_price_dispersion_usd_per_50kg"])
        self.assertEqual(1110, aa["bags_offered"])
        self.assertEqual("USD_PER_50_KG", aa["quote"])
        self.assertEqual("2026-04-08", aa["auction_sale_date"])
        self.assertEqual("completed", aa["session_status"])
        self.assertEqual("fresh", aa["freshness_state"])
        self.assertEqual(NAIROBI_COFFEE_EXCHANGE_SOURCE_URL, aa["source_url"])
        self.assertTrue(aa["price_available"])
        self.assertEqual("watch_only", aa["direction"])
        self.assertEqual("synthetic_research_only", aa["paper_route_status"])
        self.assertNotIn("candidate_reject_reason", aa)

    def test_nairobi_coffee_exchange_adapter_preserves_parser_and_fetch_evidence(self) -> None:
        malformed = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html>replacement page</html>",
            "received_at": "2026-04-10T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.nairobi_coffee_exchange.fetch_bytes", return_value=malformed
        ):
            parser_batch = NairobiCoffeeExchangeAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["market_report"]["fetch_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual(0, parser_batch.metadata["real_observation_count"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_nairobi_coffee_exchange_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "content": b"",
            "received_at": "2026-04-10T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.nairobi_coffee_exchange.fetch_bytes", return_value=unavailable
        ):
            unavailable_batch = NairobiCoffeeExchangeAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_nairobi_coffee_exchange_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_nairobi_coffee_exchange_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        target_adapter_id = "nairobi_coffee_exchange"
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": """
                Sale 25 of Wednesday, April 8, 2026
                MARKET TOTAL
                GRADE Bags offered Weight offered Min price Max price Value (USD) Average price
                AA 1,110 68,488 285.00 400.00 473,419.38 345.62
                Note Prices are in USD per 50 Kg
                NAIROBI COFFEE EXCHANGE
            """,
            "received_at": "2026-04-10T12:00:00+00:00",
            "latency_ms": 4.0,
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_nairobi() -> list[str]:
            return [
                adapter_id
                for adapter_id in original_discover()
                if adapter_id == target_adapter_id
            ]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.nairobi_coffee_exchange.fetch_bytes", return_value=result
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_nairobi):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {target_adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(1, len(batch.observations))
        self.assertEqual("NAIROBI_COFFEE_EXCHANGE", batch.observations[0]["venue"])
        self.assertEqual(345.62, batch.observations[0]["last"])
        self.assertTrue(batch.observations[0]["price_available"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(target_adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_bank_of_canada_tbill_parser_normalizes_calls_and_results(self) -> None:
        payload = {
            "seriesDetail": {
                "AUC_TBILL_ISIN": {"label": "ISIN"},
                "AUC_TBILL_AVG_YIELD": {"label": "Avg yield (%)"},
            },
            "observations": [
                {
                    "tbill_id": "19002063094-1",
                    "AUC_TBILL_KEY": {"v": "19002063094-1"},
                    "AUC_TBILL_AUCTION_DATE": {"v": "2026-07-28"},
                    "AUC_TBILL_BID_DEADLINE": {"v": "10:30"},
                    "AUC_TBILL_ISSUE_DATE": {"v": "2026-07-29"},
                    "AUC_TBILL_TERM_DAYS": {"v": "98"},
                    "AUC_TBILL_MATURITY_DATE": {"v": "2026-11-04"},
                    "AUC_TBILL_ISIN": {"v": "CA1350Z7EM25"},
                    "AUC_TBILL_AMOUNT": {"v": "16400.000"},
                    "AUC_TBILL_STATUS": {"v": "Results"},
                    "AUC_TBILL_AVG_PRICE": {"v": "99.38891"},
                    "AUC_TBILL_AVG_YIELD": {"v": "2.290"},
                    "AUC_TBILL_LOW_YIELD": {"v": "2.285"},
                    "AUC_TBILL_HIGH_YIELD": {"v": "2.298"},
                    "AUC_TBILL_COVERAGE": {"v": "1.667"},
                    "AUC_TBILL_TAIL": {"v": "0.790"},
                    "AUC_TBILL_ALLOTMENT_RATIO": {"v": "76.74580"},
                    "AUC_TBILL_BOC_PURCHASE": {"v": "164"},
                    "AUC_TBILL_TOTAL_SUBMITTED": {"v": "27337.600"},
                    "AUC_TBILL_OUTSTANDING_AFTER": {"v": "25800.000"},
                },
                {
                    "tbill_id": "19002064000-2",
                    "AUC_TBILL_AUCTION_DATE": {"v": "2026-08-11"},
                    "AUC_TBILL_BID_DEADLINE": {"v": "10:30"},
                    "AUC_TBILL_ISSUE_DATE": {"v": "2026-08-12"},
                    "AUC_TBILL_TERM_DAYS": {"v": "154"},
                    "AUC_TBILL_MATURITY_DATE": {"v": "2027-01-13"},
                    "AUC_TBILL_ISIN": {"v": "CA1350Z7FC34"},
                    "AUC_TBILL_AMOUNT": {"v": "5800.000"},
                    "AUC_TBILL_STATUS": {"v": "Final CFT"},
                    "AUC_TBILL_BOC_MIN_PURCHASE": {"v": "58.000"},
                },
                {
                    "tbill_id": "19002064000",
                    "AUC_TBILL_AUCTION_DATE": {"v": "2026-08-11"},
                    "AUC_TBILL_TOTAL_AMOUNT": {"v": "28000"},
                },
            ],
        }

        rows = parse_bank_of_canada_treasury_bill_auctions(
            payload,
            received_at="2026-08-04T16:00:00+00:00",
        )

        self.assertEqual(2, len(rows))
        result = next(row for row in rows if row["session_status"] == "results_published")
        call = next(row for row in rows if row["session_status"] == "auction_scheduled")
        self.assertEqual("BANK_OF_CANADA:CA1350Z7EM25:AUCTION:2026-07-28", result["inst_id"])
        self.assertEqual(99.38891, result["last"])
        self.assertEqual(2.298, result["stop_out_yield_pct"])
        self.assertEqual(16400.0, result["awarded_amount_millions_cad"])
        self.assertEqual(164.0, result["bank_of_canada_purchase_millions_cad"])
        self.assertEqual(1.667, result["coverage_ratio"])
        self.assertEqual(0.790, result["tail_bps"])
        self.assertEqual(98, result["term_days"])
        self.assertEqual(2.290, result["average_yield_pct"])
        self.assertEqual("official_auction_result", result["quality_status"])
        self.assertEqual(
            "official_auction_result_not_executable_quote",
            result["candidate_reject_reason"],
        )
        self.assertEqual("fresh", result["freshness_state"])
        self.assertEqual(BANK_OF_CANADA_TBILL_API_URL, result["source_url"])
        self.assertEqual("watch_only", result["direction"])
        self.assertEqual(0.0, call["last"])
        self.assertIsNone(call["awarded_amount_millions_cad"])
        self.assertEqual("official_call_for_tender", call["quality_status"])
        self.assertEqual("watch_only", call["direction"])

    def test_bank_of_canada_adapter_preserves_parser_and_fetch_evidence(self) -> None:
        reachable_bad_schema = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": '{"observations": [{"tbill_id": "overview-only"}]}',
            "received_at": "2026-08-04T16:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.bank_of_canada.fetch_text",
            return_value=reachable_bad_schema,
        ):
            parser_batch = BankOfCanadaRegularTreasuryBillsAdapter().scan({})

        self.assertEqual([], parser_batch.candidates)
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable",
            parser_batch.metadata["fetch_status"]["regular_treasury_bills"]["fetch_status"],
        )
        self.assertIn("no usable", parser_batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(parser_batch.metadata["paper_only"])
        parser_row = parser_batch.observations[0]
        self.assertEqual("watch_only", parser_row["direction"])
        self.assertEqual("public_treasury_bill_parser_failure", parser_row["candidate_reject_reason"])

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T16:01:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.bank_of_canada.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = BankOfCanadaRegularTreasuryBillsAdapter().scan({})

        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual(
            "public_treasury_bill_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])

    def test_cbb_tbill_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "central_bank_of_bahrain_treasury_bills"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("CENTRAL_BANK_OF_BAHRAIN", adapter.info.venue)
        self.assertEqual(CBB_TBILL_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("lowest_accepted_price", adapter.info.capabilities)
        self.assertIn("issue_number", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_cbb_tbill_parser_normalizes_issue_number_price_and_rate(self) -> None:
        payload = """
        <article><p>Published on 15 December 2025</p>
        <p>Manama, Bahrain – 15th December 2025 – This week’s BD 70 million issue
        of Government Treasury Bills has been oversubscribed by 101%.</p>
        <p>The bills, carrying a maturity of 91 days, are issued by the CBB.</p>
        <p>The issue date of the bills is 17<sup>th</sup> December 2025, and the maturity date
        is 18<sup>th</sup> March 2026.</p>
        <p>The weighted average rate of interest is 4.91% compared to 4.90 to the
        previous issue on 3rd December 2025.</p>
        <p>The approximate average price for the issue was 98.773% with the lowest
        accepted price being 98.727%.</p>
        <p>This is issue No.2099 (ISIN BH000NF62M89) of Government Treasury Bills.</p>
        </article>
        """
        rows = parse_central_bank_of_bahrain_treasury_bill_auction(
            payload,
            received_at="2025-12-16T12:00:00+00:00",
        )
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("CENTRAL_BANK_OF_BAHRAIN:TBILL:ISSUE:2099", row["inst_id"])
        self.assertEqual("BH000NF62M89", row["isin"])
        self.assertEqual(98.773, row["last"])
        self.assertEqual(98.727, row["lowest_accepted_price_per_100"])
        self.assertEqual(4.91, row["average_interest_rate_pct"])
        self.assertEqual(101.0, row["oversubscription_pct"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("results_published", row["session_status"])
        self.assertEqual(CBB_TBILL_RESULTS_URL, row["source_url"])
        self.assertEqual("watch_only", row["direction"])

    def test_cbb_tbill_adapter_preserves_parser_and_fetch_evidence(self) -> None:
        reachable_bad_schema = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>Government Securities reference page</body></html>",
            "received_at": "2026-08-04T16:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.central_bank_of_bahrain.fetch_text",
            return_value=reachable_bad_schema,
        ):
            parser_batch = CentralBankOfBahrainTreasuryBillsAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable",
            parser_batch.metadata["fetch_status"]["treasury_bill_results"]["fetch_status"],
        )
        self.assertIn("markers", parser_batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(parser_batch.metadata["paper_only"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_treasury_bill_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T16:01:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.central_bank_of_bahrain.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = CentralBankOfBahrainTreasuryBillsAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_treasury_bill_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_vsdc_hnx_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "vietnam_securities_depository_and_clearing_corporation_hanoi_sto"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("VSDC_HNX", adapter.info.venue)
        self.assertEqual(VSDC_CARBON_GUIDELINE_URL, adapter.info.docs_url)
        self.assertIn("settlement_cycle", adapter.info.capabilities)
        self.assertIn("carbon_credit", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_vsdc_hnx_parsers_normalize_settlement_and_operation_evidence(self) -> None:
        settlement = """
        <html><body><h3>Payment for Greenhouse Gas Emission Quota and Carbon Credit Transactions</h3>
        <p>Instant payment is made per transaction with a settlement time of T+0.</p>
        <p>Greenhouse gas emission quotas and carbon credits are transferred simultaneously
        with payment. Payment is made through BIDV Bank and transfer is carried out by VSDC.</p>
        <p>Guidelines issued together with Decision No. 17/QD-HDTV dated 29/04/2026.</p>
        </body></html>
        """
        guideline = """
        <html><body><h1>VSDC issues the Guideline on the depository and settlement of
        transactions for greenhouse gas emission allowances and carbon credits</h1>
        <p>On 29/4/2026, VSDC issued Decision 17/QD-HDTV on the Guideline.</p>
        </body></html>
        """
        coordination = """
        <html><body><h1>Signing ceremony of Memorandum on Coordination on domestic carbon
        exchange operations</h1><p>On June 22, 2026, the Hanoi Stock Exchange (HNX) and
        Vietnam Securities Depository and Clearing Corporation (VSDC) signed the MoU.</p>
        </body></html>
        """

        rows = parse_vsdc_carbon_settlement_rules(
            settlement, received_at="2026-08-04T12:00:00+00:00"
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"greenhouse_gas_emission_allowance", "carbon_credit"},
            {row["asset_class"] for row in rows},
        )
        self.assertTrue(all(row["settlement_cycle"] == "T+0" for row in rows))
        self.assertTrue(all(row["settlement_bank"] == "BIDV" for row in rows))
        self.assertTrue(all(row["delivery_versus_payment"] for row in rows))
        self.assertTrue(all(row["source_url"] == VSDC_CARBON_SETTLEMENT_URL for row in rows))
        self.assertTrue(all(row["fetch_status"] == "reachable" for row in rows))
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))

        guideline_row = parse_vsdc_carbon_guideline(
            guideline, received_at="2026-08-04T12:00:00+00:00"
        )[0]
        coordination_row = parse_hnx_carbon_coordination(
            coordination, received_at="2026-08-04T12:00:00+00:00"
        )[0]
        self.assertEqual("settlement_guideline_issued", guideline_row["session_status"])
        self.assertEqual("operations_coordination_signed", coordination_row["session_status"])
        self.assertEqual(VSDC_HNX_COORDINATION_URL, coordination_row["source_url"])

    def test_vsdc_hnx_adapter_preserves_partial_fetch_and_parser_evidence(self) -> None:
        settlement = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html>an unrelated settlement page</html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:00:01+00:00",
            "latency_ms": 3.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.vietnam_securities_depository_and_clearing_corporation_hanoi_sto.fetch_text",
            side_effect=[settlement, unavailable, unavailable],
        ):
            batch = (
                VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter().scan({})
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["settlement"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["guideline"]["fetch_status"])
        self.assertIn("required settlement markers", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))

    def test_hnx_ssc_vn2025_plugin_normalizes_calendar_and_is_runtime_discoverable(self) -> None:
        adapter_id = "hanoi_stock_exchange_state_securities_commission"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, HanoiStockExchangeStateSecuritiesCommissionAdapter)
        self.assertEqual(HNX_SSC_CARBON_PORTAL_URL, adapter.info.docs_url)
        self.assertIn("trading_calendar", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

        notice = """
        <html><body><h1>Notice on first and last trading dates of carbon emission
        allowance allocated in 2025-2026 period</h1><p>Greenhouse gas emission
        allowance code VN2025 has an allocation volume of 511,473,846 tCO2e.</p>
        <p>First trading date: 29/06/2026. Last trading date: 24/12/2027.</p>
        </body></html>
        """
        rows = parse_hnx_vn2025_trading_notice(
            notice, received_at="2026-08-04T12:00:00+00:00"
        )
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("VN2025", row["symbol"])
        self.assertEqual(511_473_846, row["allocation_volume_tco2e"])
        self.assertEqual("2025-2026", row["compliance_period"])
        self.assertEqual("2026-06-29", row["first_trading_date"])
        self.assertEqual("2027-12-24", row["last_trading_date"])
        self.assertEqual("open", row["session_status"])
        self.assertEqual(HNX_SSC_TRADING_NOTICE_URL, row["source_url"])
        self.assertEqual("watch_only", row["direction"])

    def test_hnx_ssc_adapter_preserves_fetch_and_parser_evidence(self) -> None:
        portal = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>Hanoi Stock Exchange HNX Carbon Market</body></html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        malformed_notice = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>Carbon market update</body></html>",
            "received_at": "2026-08-04T12:00:01+00:00",
            "latency_ms": 3.0,
        }
        unavailable_decree = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "content": b"",
            "received_at": "2026-08-04T12:00:02+00:00",
            "latency_ms": 4.0,
            "error": "HTTP Error 403",
        }
        unavailable_ssc_portal = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T12:00:02+00:00",
            "latency_ms": 4.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.hanoi_stock_exchange_state_securities_commission.fetch_text",
            side_effect=[portal, malformed_notice, unavailable_ssc_portal],
        ), mock.patch(
            "adapters.venues.hanoi_stock_exchange_state_securities_commission.fetch_bytes",
            return_value=unavailable_decree,
        ):
            batch = HanoiStockExchangeStateSecuritiesCommissionAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(
            "reachable", batch.metadata["fetch_status"]["trading_notice"]["fetch_status"]
        )
        self.assertEqual(
            "blocked", batch.metadata["fetch_status"]["ssc_decree"]["fetch_status"]
        )
        self.assertEqual(
            HNX_SSC_DECREE_URL, batch.metadata["fetch_status"]["ssc_decree"]["source_url"]
        )
        self.assertTrue(batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))

    def test_hnx_ssc_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        adapter_id = "hanoi_stock_exchange_state_securities_commission"
        portal = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>Hanoi Stock Exchange HNX Carbon Market</body></html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        notice = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": """<html><body>Notice: greenhouse gas emission allowance VN2025
            for the 2025-2026 period. Allocation 511,473,846 tCO2e. First trading
            date: 29/06/2026. Last trading date: 24/12/2027.</body></html>""",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        ssc_portal = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>State Securities Commission of Vietnam SSC</body></html>",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        decree = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": b"%PDF-1.7 public decree fixture",
            "received_at": "2026-08-04T12:00:00+00:00",
            "latency_ms": 2.0,
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_hnx_ssc() -> list[str]:
            return [entry for entry in original_discover() if entry == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.hanoi_stock_exchange_state_securities_commission.fetch_text",
            side_effect=[portal, notice, ssc_portal],
        ), mock.patch(
            "adapters.venues.hanoi_stock_exchange_state_securities_commission.fetch_bytes",
            return_value=decree,
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_hnx_ssc):
            batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )

        self.assertEqual(4, len(batch.observations))
        self.assertTrue(all(row["venue"] == "HNX_SSC" for row in batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

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

    def test_esx_fixed_income_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "ethiopian_securities_exchange_fixed_income"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsNotNone(adapter)
        self.assertEqual("ESX", adapter.info.venue)
        self.assertEqual(ESX_FIXED_INCOME_OVERVIEW_URL, adapter.info.docs_url)
        self.assertIn("fixed_income_instrument_catalog", adapter.info.capabilities)
        self.assertIn("trading_session", adapter.info.capabilities)
        self.assertNotIn("ticker", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_esx_fixed_income_parser_normalizes_instruments_and_session(self) -> None:
        session = parse_esx_fixed_income_session(
            _esx_fixed_income_operations_fixture(),
            received_at="2026-08-04T08:00:00+00:00",
        )
        self.assertEqual("continuous", session["session_status"])
        self.assertEqual("Africa/Addis_Ababa", session["session_timezone"])
        holiday_session = parse_esx_fixed_income_session(
            _esx_fixed_income_operations_fixture(),
            received_at="2026-08-25T08:00:00+00:00",
        )
        self.assertEqual("holiday_closed", holiday_session["session_status"])
        self.assertEqual("The Prophet's Birthday", holiday_session["holiday_name"])

        rows = parse_esx_fixed_income_instruments(
            _esx_fixed_income_instruments_fixture(),
            received_at="2026-08-04T08:00:00+00:00",
            session_status=session["session_status"],
        )
        self.assertEqual(
            [
                "GOVT_TBILL",
                "COMMERCIAL_PAPER",
                "REPO",
                "TREASURY_BOND",
                "CORPORATE_BOND",
            ],
            [row["symbol"] for row in rows],
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual([28, 91, 182, 364], by_symbol["GOVT_TBILL"]["tenor_days"])
        self.assertEqual(5000.0, by_symbol["GOVT_TBILL"]["minimum_investment_etb"])
        self.assertEqual(269, by_symbol["COMMERCIAL_PAPER"]["maximum_maturity_days"])
        self.assertEqual("continuous", by_symbol["REPO"]["session_status"])
        self.assertEqual(ESX_FIXED_INCOME_INSTRUMENTS_URL, rows[0]["source_url"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["candidate_reject_reason"] for row in rows))

    def test_esx_fixed_income_adapter_preserves_fetch_and_parser_evidence(self) -> None:
        overview = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<h1>Fixed Income Market</h1>",
            "received_at": "2026-08-04T08:00:00+00:00",
            "latency_ms": 1.0,
        }
        instruments = {
            **overview,
            "text": _esx_fixed_income_instruments_fixture(),
            "latency_ms": 2.0,
        }
        operations = {
            **overview,
            "text": _esx_fixed_income_operations_fixture(),
            "latency_ms": 3.0,
        }
        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            side_effect=[overview, instruments, operations],
        ) as fetch:
            batch = EthiopianSecuritiesExchangeFixedIncomeAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual(5, len(batch.observations))
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("continuous", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual(1296, batch.metadata["adapter_spec_id"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertEqual(
            [
                ESX_FIXED_INCOME_OVERVIEW_URL,
                ESX_FIXED_INCOME_INSTRUMENTS_URL,
                ESX_FIXED_INCOME_OPERATIONS_URL,
            ],
            [call.args[0] for call in fetch.call_args_list],
        )

        bad_instruments = {**instruments, "text": "<h1>Unrelated page</h1>"}
        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            side_effect=[overview, bad_instruments, operations],
        ):
            parser_batch = EthiopianSecuritiesExchangeFixedIncomeAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(1, len(parser_batch.metadata["parser_failures"]))
        self.assertEqual("fixed_income_instruments", parser_batch.metadata["parser_failures"][0]["parser"])
        self.assertEqual(
            "public_fixed_income_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )
        self.assertTrue(parser_batch.observations[0]["parser_failure"])

        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-04T08:00:00+00:00",
            "latency_ms": 4.0,
            "error": "HTTP Error 403",
        }
        with mock.patch(
            "adapters.venues.ethiopian_securities_exchange.fetch_text",
            side_effect=[overview, blocked, operations],
        ):
            unavailable_batch = EthiopianSecuritiesExchangeFixedIncomeAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual(
            ESX_FIXED_INCOME_INSTRUMENTS_URL,
            unavailable_batch.observations[0]["source_url"],
        )

    def test_casablanca_futures_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "casablanca_stock_exchange_futures_market"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, CasablancaStockExchangeFuturesMarketAdapter)
        self.assertEqual("CASABLANCA_FUTURES", adapter.info.venue)
        self.assertEqual(CASABLANCA_FUTURES_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("delayed_quote", adapter.info.capabilities)
        self.assertNotIn("entry_quality_quote", adapter.info.capabilities)

    def test_casablanca_futures_parser_normalizes_official_masi20_contract(self) -> None:
        document = """
        <html><body>
          <section><strong>Session closed</strong><span>Monday, June 8, 2026</span></section>
          <p>Prices are delayed by 15 minutes</p>
          <h1>MASI20 FUTURE JUI26</h1>
          <table>
            <tr><td>Ticker</td><td>FMASI20JUI26</td></tr>
            <tr><td>Contract</td><td>MASI20 FUTURE</td></tr>
            <tr><td>ISIN</td><td>MA0009000037</td></tr>
            <tr><td>Type of Underlying Asset</td><td>INDX</td></tr>
            <tr><td>Maturity</td><td>juin</td></tr>
            <tr><td>Last Trading Day</td><td>19/06/2026</td></tr>
            <tr><td>Payment Method</td><td>Cash</td></tr>
            <tr><td>Underlying Asset</td><td>MASI20</td></tr>
            <tr><td>Trading Unit (MAD)</td><td>10</td></tr>
            <tr><td>Initial Deposit</td><td>1500</td></tr>
          </table>
          <table>
            <tr><td>Price</td><td>1 320,00</td></tr>
            <tr><td>Change</td><td>0,77 %</td></tr>
            <tr><td>Opening</td><td>-</td></tr>
            <tr><td>Low</td><td>-</td></tr>
            <tr><td>High</td><td>-</td></tr>
            <tr><td>Previous closing price</td><td>1 320,00</td></tr>
            <tr><td>Volume</td><td>-</td></tr>
            <tr><td>Quantity traded</td><td>-</td></tr>
            <tr><td>Number of transactions</td><td>-</td></tr>
          </table>
        </body></html>
        """
        rows = parse_casablanca_masi20_future(
            document,
            received_at="2026-06-08T16:00:00+00:00",
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("CASABLANCA_FUTURES:FMASI20JUI26", row["inst_id"])
        self.assertEqual(1320.0, row["last"])
        self.assertEqual(0.77, row["change_pct"])
        self.assertEqual("MA0009000037", row["isin"])
        self.assertEqual("2026-06-19", row["last_trading_date"])
        self.assertEqual(10.0, row["contract_multiplier_mad_per_index_point"])
        self.assertEqual(1500.0, row["initial_deposit_mad"])
        self.assertEqual("closed", row["session_status"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("reachable", row["fetch_status"])
        self.assertEqual(CASABLANCA_FUTURES_SOURCE_URL, row["source_url"])
        self.assertEqual("watch_only", row["direction"])

        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-06-08T16:00:00+00:00",
            "latency_ms": 3.0,
        }
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_futures_market.fetch_text",
            return_value=result,
        ):
            batch = CasablancaStockExchangeFuturesMarketAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("closed", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual(956, batch.metadata["adapter_spec_id"])
        self.assertTrue(batch.metadata["paper_only"])

    def test_casablanca_futures_adapter_preserves_parser_and_fetch_failures(self) -> None:
        parser_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>replacement page</body></html>",
            "received_at": "2026-08-04T16:00:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_futures_market.fetch_text",
            return_value=parser_result,
        ):
            parser_batch = CasablancaStockExchangeFuturesMarketAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable", parser_batch.metadata["fetch_status"]["instrument"]["fetch_status"]
        )
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_futures_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )
        self.assertTrue(parser_batch.observations[0]["parser_failure"])

        unavailable_result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "text": "",
            "received_at": "2026-08-04T16:01:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.casablanca_stock_exchange_futures_market.fetch_text",
            return_value=unavailable_result,
        ):
            unavailable_batch = CasablancaStockExchangeFuturesMarketAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual(
            CASABLANCA_FUTURES_SOURCE_URL,
            unavailable_batch.observations[0]["source_url"],
        )

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

    def test_kase_global_parser_normalizes_foreign_etfs_and_adrs(self) -> None:
        html = """
        <table><tr><th>Ticker</th><th>Company</th><th>ISIN</th><th>Type</th><th>Currency</th>
        <th>Price,KZT</th><th>Volume,mln KZT</th><th>Date</th><th>Liquidity class</th><th>Market-maker</th></tr>
        <tr><td>IBIT_KZ</td><td>iShares Bitcoin Trust ETF</td><td>US46438F1012</td><td>ETF</td><td>USD</td>
        <td>36,19</td><td>2,2</td><td>04.08.2026</td><td>3</td><td>Standard Investment Company</td></tr>
        <tr><td>BABAd</td><td>Alibaba Group Holding Ltd</td><td>US01609W1027</td><td>depository receipts</td><td>USD</td>
        <td>125,78</td><td>0,060</td><td>04.08.2026</td><td>1</td><td>Standard Investment Company</td></tr>
        <tr><td>SOLZ_KZ</td><td>Volatility Shares Trust</td><td>US92864M8221</td><td>ETF</td><td>USD</td>
        <td>–</td><td>–</td><td>–</td><td>3</td><td>Standard Investment Company</td></tr></table>
        """
        rows = parse_kase_global(html, received_at="2026-08-04T18:30:00+00:00")
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(36.19, by_symbol["IBIT_KZ"]["last"])
        self.assertEqual("foreign_etf", by_symbol["IBIT_KZ"]["asset_class"])
        self.assertEqual("KASE:BABAd", by_symbol["BABAd"]["inst_id"])
        self.assertEqual("foreign_depository_receipt", by_symbol["BABAd"]["asset_class"])
        self.assertEqual(0.0, by_symbol["SOLZ_KZ"]["last"])
        self.assertEqual("no_trade_reported", by_symbol["SOLZ_KZ"]["session_status"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))

    def test_kase_global_adapter_is_discoverable_and_preserves_failure_evidence(self) -> None:
        adapter_id = "kazakhstan_stock_exchange_kase_global"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, KazakhstanStockExchangeKaseGlobalAdapter)
        self.assertEqual(KASE_GLOBAL_DOCS_URL, adapter.info.docs_url)
        self.assertNotIn("entry_quality_quote", adapter.info.capabilities)
        malformed = {
            "ok": True, "status": "reachable", "http_status": 200,
            "text": "<html><body>unrelated KASE page</body></html>",
            "received_at": "2026-08-04T18:30:00+00:00", "latency_ms": 4.0,
        }
        with mock.patch("adapters.venues.kazakhstan_stock_exchange_kase.fetch_text", return_value=malformed):
            parser_batch = KazakhstanStockExchangeKaseGlobalAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["kase_global"]["fetch_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("public_kase_global_parser_failure", parser_batch.observations[0]["candidate_reject_reason"])
        blocked = {**malformed, "ok": False, "status": "blocked", "http_status": 403, "text": "", "error": "blocked"}
        with mock.patch("adapters.venues.kazakhstan_stock_exchange_kase.fetch_text", return_value=blocked):
            unavailable_batch = KazakhstanStockExchangeKaseGlobalAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual("public_kase_global_source_unavailable", unavailable_batch.observations[0]["candidate_reject_reason"])

    def test_nzx_gdt_parser_normalizes_event_prices(self) -> None:
        html = """
        <table><tr><th>Products</th><th>Event 401</th><th>Event 400</th><th>Change</th></tr>
        <tr><td>Whole Milk Powder</td><td>3,900</td><td>3,800</td><td>+2.6%</td></tr></table>
        """
        rows = parse_nzx_gdt(html)
        self.assertEqual("NZX_GDT:WHOLE_MILK_POWDER", rows[0]["inst_id"])
        self.assertEqual(3900.0, rows[0]["last"])
        self.assertEqual("Event 401", rows[0]["event_id"])

    def test_carb_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "california_air_resources_board_cap_and_invest"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, CaliforniaAirResourcesBoardAdapter)
        self.assertEqual("CARB_CA_QC", adapter.info.venue)
        self.assertEqual(CARB_AUCTION_INFORMATION_URL, adapter.info.docs_url)
        self.assertIn("auction_settlement_price", adapter.info.capabilities)
        self.assertIn("reserve_sale", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_carb_parsers_normalize_schedule_results_vintages_and_reserve_sale(self) -> None:
        auction_page = """
        <html><body><h1>Auction Information</h1>
        <h2>May 2026 Joint Auction #47 – May 20, 2026 10:00 AM to 1:00 PM PT</h2>
        <p>Updated Auction Notice</p><p>Summary Results Report</p>
        <p>Current Auction and Advance Auction allowances will be auctioned.</p>
        </body></html>
        """
        schedule = parse_carb_auction_information(
            auction_page, received_at="2026-05-15T12:00:00+00:00"
        )
        self.assertEqual(2, len(schedule))
        self.assertEqual("CARB:CA_QC_AUCTION:47:CURRENT:2026-05-20", schedule[0]["inst_id"])
        self.assertEqual("results_published", schedule[0]["session_status"])
        self.assertTrue(schedule[0]["summary_results_report_published"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in schedule))

        results = parse_carb_auction_results(
            """
            CALIFORNIA CAP-AND-INVEST PROGRAM SUMMARY OF CALIFORNIA-QUEBEC JOINT AUCTION
            SETTLEMENT PRICES AND RESULTS
            May 2026 Joint Auction #47 May 20, 2026
            Current Auction Total Allowances Offered 49,647,415 Total Allowances Sold
            49,647,415 Current Auction Settlement Price $28.81
            Advance Auction Total Allowances Offered 6,481,750 Total Allowances Sold
            6,481,750 Advance Auction Settlement Price $28.76
            """,
            received_at="2026-05-27T19:00:00+00:00",
        )
        current = next(row for row in results if row["allowance_category"] == "current")
        advance = next(row for row in results if row["allowance_category"] == "advance")
        self.assertEqual(28.81, current["auction_settlement_price_usd"])
        self.assertEqual(49_647_415.0, current["allowances_sold"])
        self.assertEqual(28.76, advance["last"])
        self.assertEqual("closed", advance["session_status"])

        notice = parse_carb_auction_notice(
            """
            California Cap-and-Invest Program Notice of May 2026 Joint Auction #47
            Auction Date May 20, 2026. Current Auction vintage 2024 and vintage 2026.
            Advance Auction vintage 2029. Price Containment Reserve Sale vintage 2026.
            """,
            received_at="2026-03-21T12:00:00+00:00",
        )
        reserve = next(row for row in notice if row["allowance_category"] == "reserve")
        self.assertEqual((2026,), reserve["vintage_years"])
        self.assertTrue(reserve["reserve_sale"])
        self.assertEqual(CARB_MAY_2026_NOTICE_URL, parse_carb_auction_notice.__kwdefaults__["source_url"])

    def test_carb_adapter_preserves_parser_and_fetch_evidence(self) -> None:
        auction_page = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<h1>Auction Information</h1><p>May 2026 Joint Auction #47 – May 20, 2026</p><p>Current Auction Advance Auction</p>",
            "received_at": "2026-05-15T12:00:00+00:00",
            "latency_ms": 3.0,
        }
        malformed_dashboard = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>replacement page</body></html>",
            "received_at": "2026-05-15T12:00:01+00:00",
            "latency_ms": 3.0,
        }
        result_pdf = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "content": b"",
            "received_at": "2026-05-15T12:00:02+00:00",
            "latency_ms": 3.0,
        }
        notice_pdf = {**result_pdf, "status": "unavailable", "http_status": 503}
        with mock.patch(
            "adapters.venues.california_air_resources_board.fetch_text",
            side_effect=[auction_page, malformed_dashboard],
        ), mock.patch(
            "adapters.venues.california_air_resources_board.fetch_bytes",
            side_effect=[result_pdf, notice_pdf],
        ):
            batch = CaliforniaAirResourcesBoardAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["auction_information"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["results_summary"]["fetch_status"])
        self.assertIn("data dashboard markers", batch.metadata["parser_failures"][0]["error"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))
        self.assertTrue(any(row.get("source_url") == CARB_DATA_DASHBOARD_FILES_URL for row in batch.observations))
        self.assertTrue(any(row.get("source_url") == CARB_RESULTS_SUMMARY_URL for row in batch.observations))

    def test_eex_eu_ets_plugin_is_runtime_discoverable_and_watch_only(self) -> None:
        self.assertIn("eex_eua_primary_auction_spot_public", discover_adapters())
        adapter = get_adapter("eex_eua_primary_auction_spot_public")
        self.assertIsNotNone(adapter)
        self.assertEqual("EEX", adapter.info.venue)
        self.assertIn("datasource_getAuction", adapter.info.capabilities)
        self.assertIn("datasource_getSpot", adapter.info.capabilities)
        self.assertIn("public_market_data_file", adapter.info.capabilities)
        self.assertNotIn("candidate_generation", adapter.info.capabilities)

    def test_aib_eex_france_cpb_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "aib_eex_france_biomethane_cpb_public"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, AibEexFranceBiomethaneCpbAdapter)
        self.assertEqual("AIB_EEX_FRANCE", adapter.info.venue)
        self.assertEqual(AIB_EEX_FRANCE_SOURCE_URL, adapter.info.docs_url)
        self.assertIn("monthly_issued_certificate_list", adapter.info.capabilities)
        self.assertIn("monthly_average_transaction_price", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_aib_eex_france_cpb_protocol_parser_normalizes_monthly_reporting_surface(self) -> None:
        rows = parse_aib_eex_france_cpb_reporting(
            _aib_eex_france_cpb_protocol_fixture(),
            received_at="2026-02-01T12:00:00+00:00",
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("AIB_EEX_FRANCE:CPB:MONTHLY_REPORTING", row["inst_id"])
        self.assertEqual("france_biomethane_cpb_monthly_reporting", row["market_surface"])
        self.assertTrue(row["monthly_issued_list_available"])
        self.assertTrue(row["monthly_average_purchase_sale_price_available"])
        self.assertEqual("2026-01-21", row["protocol_date"])
        self.assertIsNone(row["reported_price"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(AIB_EEX_FRANCE_SOURCE_URL, row["source_url"])

    def test_aib_eex_france_cpb_workbook_parser_normalizes_price_and_issuance_rows(self) -> None:
        rows = parse_aib_eex_france_cpb_workbook(
            _aib_eex_france_cpb_workbook_fixture(),
            source_url="https://www.eex.com/fileadmin/WEB_CPBs_Janvier_2026.xlsx",
            received_at="2026-02-15T12:00:00+00:00",
        )
        price = next(row for row in rows if row["trade_type"] == "official_monthly_average_transaction_price")
        issuance = next(row for row in rows if row["trade_type"] == "official_monthly_issued_certificate_list")
        self.assertEqual("AIB_EEX_FRANCE:CPB:AVERAGE_PRICE:2026:2026-01", price["inst_id"])
        self.assertEqual(83.03, price["last"])
        self.assertEqual("2026-01", price["reporting_month"])
        self.assertEqual("2026-06-16", issuance["issuance_date"])
        self.assertEqual(7371.0, issuance["issued_cpb_volume"])
        self.assertEqual(7371.0, issuance["issued_biomethane_mwh"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))

    def test_aib_eex_france_cpb_adapter_preserves_fetch_and_parser_evidence(self) -> None:
        publication_page = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": '<a href="/fileadmin/EEX/Downloads/Registry_Services/French_registry_for_Biogas_Production_Certificates/WEB_CPBs_Janvier_2026.xlsx">xlsx</a>',
            "received_at": "2026-02-01T12:00:00+00:00",
            "latency_ms": 3.0,
        }
        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": _aib_eex_france_cpb_workbook_fixture(),
            "received_at": "2026-02-01T12:00:01+00:00",
            "latency_ms": 3.0,
            "content_type": "application/pdf",
        }
        with mock.patch("adapters.venues.aib_eex_france.fetch_text", return_value=publication_page), mock.patch(
            "adapters.venues.aib_eex_france.fetch_bytes", return_value=reachable
        ):
            batch = AibEexFranceBiomethaneCpbAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual(1512, batch.metadata["adapter_spec_id"])
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(
            "reachable", batch.metadata["fetch_status"]["cpb_publication_page"]["fetch_status"]
        )
        self.assertEqual(
            "reachable", batch.metadata["fetch_status"]["cpb_issuance_price_workbook"]["fetch_status"]
        )
        self.assertEqual("monthly_reported", batch.metadata["session_state"])
        self.assertEqual(3, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        malformed = {**reachable, "content": b"not an xlsx"}
        with mock.patch("adapters.venues.aib_eex_france.fetch_text", return_value=publication_page), mock.patch(
            "adapters.venues.aib_eex_france.fetch_bytes", return_value=malformed
        ):
            parser_batch = AibEexFranceBiomethaneCpbAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["cpb_issuance_price_workbook"]["fetch_status"])
        self.assertEqual(
            "public_cpb_reporting_protocol_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        blocked = {**publication_page, "ok": False, "status": "blocked", "http_status": 403, "text": ""}
        with mock.patch("adapters.venues.aib_eex_france.fetch_text", return_value=blocked):
            unavailable_batch = AibEexFranceBiomethaneCpbAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual(EEX_CPB_PAGE_URL, unavailable_batch.observations[0]["source_url"])

    def test_eex_uka_plugin_is_runtime_discoverable_and_paper_only(self) -> None:
        adapter_id = "eex_uka_futures_options_public"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, EexUkaFuturesOptionsAdapter)
        self.assertEqual("EEX", adapter.info.venue)
        self.assertEqual(EEX_UK_ETS_PAGE_URL, adapter.info.docs_url)
        self.assertIn("contract_catalog", adapter.info.capabilities)
        self.assertNotIn("ticker", adapter.info.capabilities)
        self.assertNotIn("order_book", adapter.info.capabilities)

    def test_eex_uka_parser_normalizes_december_2026_futures_and_options(self) -> None:
        rows = parse_eex_uka_futures_options(
            _eex_uka_contract_fixture(),
            received_at="2026-08-04T16:00:00+00:00",
        )

        self.assertEqual(2, len(rows))
        futures, options = rows
        self.assertEqual("EEX:UKA:FUTURE:DEC2026", futures["inst_id"])
        self.assertEqual("futures_catalog", futures["market_type"])
        self.assertEqual("2026-12", futures["first_tradeable_expiry"])
        self.assertEqual(1_000, futures["contract_volume_uka"])
        self.assertEqual(0.01, futures["minimum_tick_gbp_per_uka"])
        self.assertEqual("EEX:UKA:OPTION:DEC2026", options["inst_id"])
        self.assertEqual("European", options["option_style"])
        self.assertEqual("reference_only", options["session_status"])
        self.assertTrue(all(row["last"] == 0.0 for row in rows))
        self.assertTrue(all(row["direction"] == "watch_only" for row in rows))
        self.assertTrue(all(row["source_url"] == EEX_UK_ETS_PAGE_URL for row in rows))

    def test_eex_uka_adapter_retains_source_and_parser_health(self) -> None:
        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": _eex_uka_contract_fixture(),
            "received_at": "2026-08-04T16:00:00+00:00",
            "latency_ms": 3.0,
        }
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            return_value=reachable,
        ):
            batch = EexUkaFuturesOptionsAdapter().scan({})
        self.assertEqual([], batch.candidates)
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["contract_specification"]["fetch_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("reference_only", batch.metadata["session_state"])
        self.assertEqual(2, batch.metadata["real_observation_count"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        malformed = {**reachable, "text": "<html><body>replacement page</body></html>"}
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            return_value=malformed,
        ):
            parser_batch = EexUkaFuturesOptionsAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("reachable", parser_batch.metadata["fetch_status"]["contract_specification"]["fetch_status"])
        self.assertEqual("public_reference_parser_failure", parser_batch.observations[0]["candidate_reject_reason"])

        blocked = {**reachable, "ok": False, "status": "blocked", "http_status": 403, "text": ""}
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            return_value=blocked,
        ):
            unavailable_batch = EexUkaFuturesOptionsAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual("blocked", unavailable_batch.observations[0]["fetch_status"])
        self.assertEqual(EEX_UK_ETS_PAGE_URL, unavailable_batch.observations[0]["source_url"])

    def test_eex_get_auction_parser_normalizes_documented_json(self) -> None:
        source_url = datasource_auction_url("2026-08-04")
        rows = parse_eex_eu_ets_auction(
            {
                "results": [
                    {
                        "result": [
                            {
                                "Time": "11:00:35",
                                "AuctionName": "EEX EUA Primary Auction Spot",
                                "Contract": "T3PA",
                                "Status": "Successful",
                                "AuctionClearingPrice": "80,86",
                                "MinimumBid": "0,01",
                                "MaximumBid": "85,00",
                                "Mean": "79,75",
                                "Median": "80,43",
                                "AuctionVolume": 2_246_500,
                                "TotalVolumeOfBidsSubmitted": 4_013_000,
                                "NumberOfBidsSubmitted": 116,
                                "NumberOfSuccessfulBids": 20,
                                "CoverRatio": "1,79",
                                "TotalNumberOfBidders": 22,
                                "NumberOfSuccessfulBidders": 14,
                                "TotalRevenue": 181_651_990,
                                "CountryRevenue": "EU",
                            }
                        ]
                    }
                ]
            },
            trade_date="2026-08-04",
            source_url=source_url,
            received_at="2026-08-04T10:00:00+00:00",
        )

        row = rows[0]
        self.assertEqual("EEX:EUA:PRIMARY_AUCTION:2026-08-04:EU", row["inst_id"])
        self.assertEqual(80.86, row["last"])
        self.assertEqual(2_246_500.0, row["auction_volume"])
        self.assertEqual(4_013_000.0, row["total_volume_of_bids"])
        self.assertEqual(1_766_500.0, row["bid_volume_excess"])
        self.assertEqual(1.79, row["cover_ratio"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("closed", row["session_status"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(source_url, row["source_url"])

    def test_eex_public_workbook_parser_supports_euaa(self) -> None:
        rows = parse_eex_eua_auction_workbook(
            _eex_auction_workbook_fixture(),
            source_url="https://public.eex-group.com/example-2026.xlsx",
            received_at="2026-08-04T10:00:00+00:00",
        )

        row = rows[0]
        self.assertEqual("EUAA", row["allowance_type"])
        self.assertEqual("EEX:EUAA:PRIMARY_AUCTION:2026-08-04:EU", row["inst_id"])
        self.assertEqual(80.86, row["auction_clearing_price"])
        self.assertEqual(116, row["bid_count"])
        self.assertEqual(14, row["successful_bidder_count"])
        self.assertEqual("public_xlsx_market_data_file", row["source_record_type"])
        self.assertEqual("watch_only", row["direction"])

    def test_eex_get_spot_parser_normalizes_euaa_trade(self) -> None:
        source_url = datasource_spot_url("2026-08-04")
        rows = parse_eex_emissions_spot(
            {
                "results": [
                    {
                        "result": [
                            {
                                "Product": "/E.SEMAZ29",
                                "Root": "SEMA",
                                "LongName": "EEX EUAA Spot",
                                "MarketArea": "EU",
                                "TradeTimestamp": "2026-08-04T09:11:05Z",
                                "TradeID": "691200",
                                "ValidTrade": "Yes",
                                "Price": "80,500",
                                "TradedVolume": 1_000,
                                "UnitOfVolumes": "tCO2",
                                "TradedType": "EXCHANGE",
                            }
                        ]
                    }
                ]
            },
            source_url=source_url,
            received_at="2026-08-04T10:00:00+00:00",
        )

        row = rows[0]
        self.assertEqual("EEX:EUAA:SPOT:691200", row["inst_id"])
        self.assertEqual(80.5, row["last"])
        self.assertEqual(1_000.0, row["traded_volume"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(source_url, row["source_url"])

    def test_eex_eu_ets_adapter_uses_public_file_when_apis_require_auth(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 401,
            "received_at": "2026-08-04T10:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 401",
        }
        workbook = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": _eex_auction_workbook_fixture(),
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "received_at": "2026-08-04T10:00:01+00:00",
            "latency_ms": 8.0,
        }
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            side_effect=[blocked, blocked],
        ), mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_bytes",
            return_value=workbook,
        ):
            batch = EexEuaPrimaryAuctionSpotAdapter().scan(
                {"public_market_adapters": {"eex_eua_primary_auction_spot_public": {"trade_date": "2026-08-04"}}}
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["auction_api"]["fetch_status"])
        self.assertEqual("blocked", batch.metadata["fetch_status"]["spot_api"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["auction_file"]["fetch_status"])
        self.assertEqual(1, batch.metadata["real_observation_count"])
        self.assertEqual(1, batch.metadata["auction_observation_count"])
        self.assertEqual(0, batch.metadata["spot_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertEqual(2, sum(row.get("quality_status") == "source_health" for row in batch.observations))

    def test_eex_eu_ets_adapter_preserves_workbook_parser_failure(self) -> None:
        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 401,
            "received_at": "2026-08-04T10:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 401",
        }
        malformed_workbook = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": b"not an xlsx archive",
            "received_at": "2026-08-04T10:00:01+00:00",
            "latency_ms": 8.0,
        }
        with mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_text",
            side_effect=[blocked, blocked],
        ), mock.patch(
            "adapters.venues.european_energy_exchange_eex.fetch_bytes",
            return_value=malformed_workbook,
        ):
            batch = EexEuaPrimaryAuctionSpotAdapter().scan(
                {"public_market_adapters": {"eex_eua_primary_auction_spot_public": {"trade_date": "2026-08-04"}}}
            )

        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertIn("invalid XLSX", batch.metadata["parser_failures"][0]["error"])
        parser_health = [row for row in batch.observations if row.get("parser_failure")]
        self.assertEqual(1, len(parser_health))
        self.assertEqual("public_reference_parser_failure", parser_health[0]["candidate_reject_reason"])

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

    def test_enegem_parser_normalizes_official_cross_border_programme(self) -> None:
        document = """
        <html><body>
          <h1>Energy Exchange Malaysia ENEGEM</h1>
          <p>Energy Exchange Malaysia (ENEGEM) is a platform developed to support
          cross-border trading of renewable energy. It enables Malaysia to export
          excess green electricity under the Cross-Border Electricity Sales for
          Renewable Energy (CBES RE) initiative, approved by the Government in
          October 2023 with a capacity of up to 300 MW.</p>
          <p>The platform is operated by the Single Buyer under the oversight of the
          Energy Commission. Its implementation is guided by the Guide for
          Cross-Border Electricity Sales (CBES), Third Edition released in April 2024.</p>
          <p>The first phase used the Malaysia Singapore interconnection for a one
          year supply period.</p>
        </body></html>
        """

        rows = parse_enegem_programme(
            document,
            received_at="2026-08-04T16:30:00+00:00",
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("ST_ENEGEM:CBES_RE:MYS_SGP", row["inst_id"])
        self.assertEqual(300.0, row["export_capacity_limit_mw"])
        self.assertEqual("2023-10-01", row["approval_month"])
        self.assertEqual("2024-04-01", row["guide_release_month"])
        self.assertEqual("official_programme_reference", row["session_status"])
        self.assertEqual("fresh", row["freshness_state"])
        self.assertEqual(ENEGEM_SOURCE_URL, row["source_url"])
        self.assertEqual("watch_only", row["direction"])
        self.assertEqual(0.0, row["last"])
        self.assertEqual(
            "official_programme_page_has_no_clearing_price",
            row["candidate_reject_reason"],
        )

        reachable_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.suruhanjaya_tenaga_energy_commission.fetch_text",
            return_value=reachable_result,
        ):
            batch = SuruhanjayaTenagaEnergyCommissionAdapter().scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("official_programme_reference", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertEqual("official_programme_reference", batch.observations[0]["quality_status"])

    def test_enegem_plugin_is_runtime_discoverable_and_preserves_failures(self) -> None:
        self.assertIn("suruhanjaya_tenaga_energy_commission", discover_adapters())
        discovered = get_adapter("suruhanjaya_tenaga_energy_commission")
        self.assertIsInstance(discovered, SuruhanjayaTenagaEnergyCommissionAdapter)
        self.assertEqual(ENEGEM_SOURCE_URL, discovered.info.docs_url)
        self.assertIn("export_capacity", discovered.info.capabilities)

        parser_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>replacement page</body></html>",
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.suruhanjaya_tenaga_energy_commission.fetch_text",
            return_value=parser_result,
        ):
            parser_batch = SuruhanjayaTenagaEnergyCommissionAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable", parser_batch.metadata["fetch_status"]["enegem"]["fetch_status"]
        )
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_programme_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable_result = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "text": "",
            "received_at": "2026-08-04T16:31:00+00:00",
            "latency_ms": 7.0,
        }
        with mock.patch(
            "adapters.venues.suruhanjaya_tenaga_energy_commission.fetch_text",
            return_value=unavailable_result,
        ):
            unavailable_batch = SuruhanjayaTenagaEnergyCommissionAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_programme_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_india_treds_pib_parser_normalizes_platform_and_policy_evidence(self) -> None:
        document = """
        <html><body>
          <p>Ministry of Micro,Small &amp; Medium Enterprises</p>
          <h1>Faster Payments, Stronger MSME: Government Mandates TReDS</h1>
          <p>Posted On: 10 JUL 2026 11:43AM by PIB Delhi</p>
          <p>Mandatory settlement through TReDS: All operating Central Public Sector
          Enterprises (CPSEs) must route the settlement of invoices for goods and
          services procured from MSMEs through TReDS platforms authorised by the
          Reserve Bank of India (RBI).</p>
          <p>The Ministry of MSME has notified mandatory use of the Trade Receivables
          Discounting System (TReDS). The Notification, issued on 30 June 2026,
          gives effect to the policy.</p>
          <p>TReDS is an RBI-regulated electronic platform, operational since 2017,
          for financing and discounting the trade receivables of MSMEs due from
          corporate buyers, Government Departments and Public Sector Undertakings,
          through competitive bidding by multiple financiers. Five platforms are
          currently operational: RXIL, M1xchange, Invoicemart, C2treds and DTX.
          The platform has grown from strength to strength, with invoice discounting
          increasing from ₹40,000 crore in FY 2021-22 to ₹3.47 lakh crore in FY 2025-26.</p>
        </body></html>
        """
        rows = parse_india_treds_pib_release(
            document, received_at="2026-08-04T16:30:00+00:00"
        )

        self.assertEqual(5, len(rows))
        rxil = rows[0]
        self.assertEqual("INDIA_TREDS:RXIL", rxil["inst_id"])
        self.assertEqual("RXIL", rxil["platform"])
        self.assertTrue(rxil["cpse_mandatory_treds_routing"])
        self.assertEqual("2026-06-30", rxil["notification_date"])
        self.assertEqual("2026-07-10", rxil["release_date"])
        self.assertEqual(40_000.0, rxil["invoice_discounting_start_crore_inr"])
        self.assertEqual(347_000.0, rxil["invoice_discounting_end_crore_inr"])
        self.assertEqual("official_policy_reference", rxil["session_status"])
        self.assertEqual("fresh", rxil["freshness_state"])
        self.assertEqual(INDIA_TREDS_PIB_SOURCE_URL, rxil["source_url"])
        self.assertEqual("watch_only", rxil["direction"])
        self.assertEqual(0.0, rxil["last"])
        self.assertEqual(
            "public_treds_release_has_no_invoice_level_discount_rate",
            rxil["candidate_reject_reason"],
        )

        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 4.0,
        }
        with mock.patch(
            "adapters.venues.government_of_india_ministry_of_msme_pib.fetch_text",
            return_value=reachable,
        ):
            batch = GovernmentOfIndiaMinistryOfMsmePibAdapter().scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["pib_treds_release"]["fetch_status"])
        self.assertEqual("fresh", batch.metadata["freshness_state"])
        self.assertEqual("official_policy_reference", batch.metadata["session_state"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

    def test_india_treds_pib_plugin_is_runtime_discoverable_and_preserves_failures(self) -> None:
        adapter_id = "government_of_india_ministry_of_msme_pib"
        self.assertIn(adapter_id, discover_adapters())
        discovered = get_adapter(adapter_id)
        self.assertIsInstance(discovered, GovernmentOfIndiaMinistryOfMsmePibAdapter)
        self.assertEqual(INDIA_TREDS_PIB_SOURCE_URL, discovered.info.docs_url)
        self.assertIn("invoice_discounting_platform_catalog", discovered.info.capabilities)

        parser_result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>replacement page</body></html>",
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.government_of_india_ministry_of_msme_pib.fetch_text",
            return_value=parser_result,
        ):
            parser_batch = GovernmentOfIndiaMinistryOfMsmePibAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable", parser_batch.metadata["fetch_status"]["pib_treds_release"]["fetch_status"]
        )
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_treds_policy_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "text": "",
            "received_at": "2026-08-04T16:31:00+00:00",
            "latency_ms": 7.0,
        }
        with mock.patch(
            "adapters.venues.government_of_india_ministry_of_msme_pib.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = GovernmentOfIndiaMinistryOfMsmePibAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])
        self.assertEqual(
            "public_treds_policy_source_unavailable",
            unavailable_batch.observations[0]["candidate_reject_reason"],
        )

    def test_india_treds_pib_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        adapter_id = "government_of_india_ministry_of_msme_pib"
        document = """
        <html><body>Ministry of Micro,Small &amp; Medium Enterprises
        Posted On: 10 JUL 2026 11:43AM
        All operating Central Public Sector Enterprises (CPSEs) must use mandatory
        settlement through TReDS. Trade Receivables Discounting System (TReDS) is an
        RBI-regulated electronic platform through competitive bidding by multiple
        financiers. Notification dated 30 June 2026. Five platforms are currently
        operational: RXIL, M1xchange, Invoicemart, C2treds and DTX. Invoice discounting
        increasing from ₹40,000 crore in FY 2021-22 to ₹3.47 lakh crore in FY 2025-26.
        </body></html>
        """
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 4.0,
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_india_treds() -> list[str]:
            return [identifier for identifier in original_discover() if identifier == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.government_of_india_ministry_of_msme_pib.fetch_text",
            return_value=result,
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_india_treds):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(5, len(batch.observations))
        self.assertTrue(all(row["venue"] == "INDIA_TREDS" for row in batch.observations))
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_uae_mof_issuance_programme_parser_normalizes_t_bonds_and_t_sukuk(self) -> None:
        document = """
        <html><body><h1>Issuance Programme</h1>
        <h2>Institutional Issuance Calendar - Year 2026</h2>
        <table>
          <tr><th>Security Type</th><th>ISIN</th><th>Maturity Date</th>
              <th>Total Outstanding Domestic Debt (As of 31st December 2025)</th>
              <th>27th Jan</th><th>17th Feb</th><th>Total AED Issuances for Year 2026</th>
              <th>Institutional Redemptions in 2026</th>
              <th>Total Outstanding Institutional Domestic Debt (As of 29th July 2026)</th>
              <th>Total Outstanding Institutional Domestic Debt (As of 31st December 2026)</th></tr>
          <tr><td>T-Bonds</td><td>AED01089C228</td><td>14 Sep 2027</td><td>2,050</td>
              <td>550</td><td>â€“</td><td>550</td><td>â€“</td><td>2,600</td><td>2,600</td></tr>
          <tr><td>T-Sukuk</td><td>AED01283C235</td><td>24 Aug 2028</td><td>4,400</td>
              <td>â€“</td><td>550</td><td>550</td><td>â€“</td><td>4,950</td><td>4,950</td></tr>
        </table>
        <h2>Retail Issuance Calendar - Year 2026</h2>
        <table>
          <tr><th>Security Type</th><th>ISIN</th><th>Maturity Date</th>
              <th>Total Outstanding Domestic Debt</th><th>July</th><th>Aug</th>
              <th>Total AED Issuances for Year 2026</th><th>Retail Redemptions in 2026</th>
              <th>Total Outstanding Retail Domestic Debt</th><th>Total Outstanding Retail Domestic Debt</th></tr>
          <tr><td>T-Sukuk</td><td>TBA</td><td>1 July 2028</td><td>â€“</td>
              <td>100</td><td>â€“</td><td>100</td><td>â€“</td><td>100</td><td>100</td></tr>
        </table></body></html>
        """
        rows = parse_uae_federal_debt_issuance_programme(
            document, received_at="2026-08-04T16:30:00+00:00"
        )

        self.assertEqual(3, len(rows))
        bond = rows[0]
        self.assertEqual("UAE_MOF_FDMO:T_BONDS:AED01089C228", bond["inst_id"])
        self.assertEqual("AED01089C228", bond["isin"])
        self.assertEqual("14 Sep 2027", bond["maturity_date"])
        self.assertEqual("2027-09-14", bond["maturity_date_iso"])
        self.assertEqual(2600.0, bond["total_outstanding_domestic_debt_millions_aed"])
        self.assertEqual("27th Jan", bond["scheduled_issuance_tranches"][0]["auction_label"])
        self.assertEqual("official_issuance_calendar_reference", bond["quality_status"])
        self.assertEqual("watch_only", bond["direction"])
        self.assertEqual("retail", rows[-1]["calendar_segment"])
        self.assertEqual("TBA", rows[-1]["isin"])

    def test_uae_mof_plugin_is_runtime_discoverable_and_preserves_failures(self) -> None:
        adapter_id = "ministry_of_finance_uae_federal_debt_management_office"
        self.assertIn(adapter_id, discover_adapters())
        discovered = get_adapter(adapter_id)
        self.assertIsInstance(discovered, MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter)
        self.assertEqual(UAE_MOF_ISSUANCE_PROGRAMME_URL, discovered.info.docs_url)
        self.assertIn("outstanding_debt_reference", discovered.info.capabilities)

        reachable = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": "<html><body>Issuance Programme</body></html>",
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.ministry_of_finance_uae_federal_debt_management_office.fetch_text",
            return_value=reachable,
        ):
            parser_batch = MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter().scan({})
        self.assertEqual("degraded", parser_batch.metadata["source_status"])
        self.assertEqual(
            "reachable", parser_batch.metadata["fetch_status"]["issuance_programme"]["fetch_status"]
        )
        self.assertTrue(parser_batch.metadata["parser_failures"])
        self.assertEqual("watch_only", parser_batch.observations[0]["direction"])
        self.assertEqual(
            "public_issuance_calendar_parser_failure",
            parser_batch.observations[0]["candidate_reject_reason"],
        )

        unavailable = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "error": "blocked",
            "text": "",
            "received_at": "2026-08-04T16:31:00+00:00",
            "latency_ms": 7.0,
        }
        with mock.patch(
            "adapters.venues.ministry_of_finance_uae_federal_debt_management_office.fetch_text",
            return_value=unavailable,
        ):
            unavailable_batch = MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter().scan({})
        self.assertEqual("blocked", unavailable_batch.metadata["source_status"])
        self.assertEqual([], unavailable_batch.metadata["parser_failures"])
        self.assertEqual("unknown", unavailable_batch.metadata["freshness_state"])
        self.assertEqual("unknown", unavailable_batch.metadata["session_state"])
        self.assertEqual("watch_only", unavailable_batch.observations[0]["direction"])

    def test_uae_mof_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        adapter_id = "ministry_of_finance_uae_federal_debt_management_office"
        document = """
        <html><body><h1>Issuance Programme</h1><p>Issuance Calendar - Year 2026</p>
        <table><tr><th>Security Type</th><th>ISIN</th><th>Maturity Date</th>
        <th>Total Outstanding Domestic Debt</th><th>27th Jan</th>
        <th>Total AED Issuances for Year 2026</th><th>Redemptions</th>
        <th>Total Outstanding Domestic Debt</th><th>Total Outstanding Domestic Debt</th></tr>
        <tr><td>T-Bonds</td><td>AED01089C228</td><td>14 Sep 2027</td><td>2,050</td>
        <td>550</td><td>550</td><td>â€“</td><td>2,600</td><td>2,600</td></tr></table>
        </body></html>
        """
        result = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "text": document,
            "received_at": "2026-08-04T16:30:00+00:00",
            "latency_ms": 4.0,
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_uae() -> list[str]:
            return [adapter_id for adapter_id in original_discover() if adapter_id == "ministry_of_finance_uae_federal_debt_management_office"]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.ministry_of_finance_uae_federal_debt_management_office.fetch_text",
            return_value=result,
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_uae):
            batch = adapter_runtime.build_scan_batch(
                {
                    "public_market_adapters": {
                        "enabled": True,
                        "workers": 1,
                        "adapters": {adapter_id: {"cache_minutes": 0}},
                    }
                }
            )

        self.assertEqual(1, len(batch.observations))
        self.assertEqual("UAE_MOF_FDMO", batch.observations[0]["venue"])
        self.assertEqual("watch_only", batch.observations[0]["direction"])
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_aofm_parsers_normalize_scheduled_tender_and_result_workbook(self) -> None:
        forthcoming = """
        <html><body><h1>Forthcoming Transactions</h1><h2>Treasury Bonds</h2>
        <table>
          <tr><th>Series Offered</th><td>3.00% 21 March 2047</td><td>4.25% 21 October 2036</td></tr>
          <tr><th>Offered to Public ($million)</th><td>1,000</td><td>800</td></tr>
          <tr><th>ISIN</th><td>AU000XCLWAM8</td><td>AU000XCLWAF4</td></tr>
          <tr><th>Tender Date</th><td>Wednesday, 5 August 2026</td><td>Friday, 7 August 2026</td></tr>
          <tr><th>Time to Submit Bids</th><td>10:45 - 11:00 AM AEST</td><td>10:45 - 11:00 AM AEST</td></tr>
          <tr><th>Settlement Date</th><td>Friday, 7 August 2026</td><td>Tuesday, 11 August 2026</td></tr>
        </table></body></html>
        """
        scheduled = parse_aofm_forthcoming_transactions(
            forthcoming, received_at="2026-08-04T12:00:00+00:00"
        )
        self.assertEqual(2, len(scheduled))
        self.assertEqual("AU000XCLWAM8", scheduled[0]["isin"])
        self.assertEqual(1000.0, scheduled[0]["offered_to_public_millions_aud"])
        self.assertEqual("tender_scheduled", scheduled[0]["session_status"])
        self.assertEqual("watch_only", scheduled[0]["direction"])

        results = parse_aofm_treasury_bond_issuance_workbook(
            _aofm_tender_results_workbook_fixture(),
            received_at="2026-08-06T12:00:00+00:00",
        )
        self.assertEqual(1, len(results))
        self.assertEqual("AU000XCLWAM8", results[0]["isin"])
        self.assertEqual("3.00% 21 March 2047", results[0]["series_offered"])
        self.assertEqual("TB2026-12", results[0]["tender_number"])
        self.assertEqual(0.0, results[0]["last"])
        self.assertEqual(4.321, results[0]["weighted_average_yield_pct"])
        self.assertEqual(1000.0, results[0]["allotted_millions_aud"])
        self.assertEqual("results_published", results[0]["session_status"])

    def test_aofm_adapter_preserves_parser_failure_as_watch_only_health_evidence(self) -> None:
        reachable_schedule = {
            "ok": True, "status": "reachable", "http_status": 200,
            "text": "<html><body><h1>Treasury Bonds</h1></body></html>",
            "received_at": "2026-08-04T16:00:00+00:00", "latency_ms": 4.0,
        }
        reachable_bad_hub = {
            "ok": True, "status": "reachable", "http_status": 200,
            "text": "<html><body>Data Hub</body></html>",
            "received_at": "2026-08-04T16:00:01+00:00", "latency_ms": 5.0,
        }
        with mock.patch(
            "adapters.venues.australian_office_of_financial_management_aofm.fetch_text",
            side_effect=[reachable_schedule, reachable_bad_hub],
        ):
            batch = AustralianOfficeOfFinancialManagementAofmAdapter().scan({})
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["data_hub"]["fetch_status"])
        self.assertTrue(batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row.get("parser_failure") for row in batch.observations))

    def test_aofm_plugin_is_auto_discovered_by_adapter_runtime(self) -> None:
        adapter_id = "australian_office_of_financial_management_aofm"
        forthcoming = """
        <html><body><h1>Forthcoming Transactions</h1><h2>Treasury Bonds</h2><table>
        <tr><th>Series Offered</th><td>2.75% 21 November 2028</td></tr>
        <tr><th>Offered to Public ($million)</th><td>1,000</td></tr>
        <tr><th>ISIN</th><td>AU000XCLWAA9</td></tr>
        <tr><th>Tender Date</th><td>Wednesday, 5 August 2026</td></tr>
        <tr><th>Time to Submit Bids</th><td>10:45 - 11:00 AM AEST</td></tr>
        <tr><th>Settlement Date</th><td>Friday, 7 August 2026</td></tr>
        </table></body></html>
        """
        data_hub = '<html><body><a href="/files/treasury%20bonds%20-%20issuance.xlsx">treasury bonds - issuance.xlsx</a></body></html>'
        text_result = lambda text: {
            "ok": True, "status": "reachable", "http_status": 200, "text": text,
            "received_at": "2026-08-04T16:30:00+00:00", "latency_ms": 4.0,
        }
        workbook_result = {
            "ok": True, "status": "reachable", "http_status": 200,
            "content": _aofm_tender_results_workbook_fixture(),
            "received_at": "2026-08-04T16:30:01+00:00", "latency_ms": 5.0,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        original_discover = adapter_runtime.discover_adapters

        def discover_only_aofm() -> list[str]:
            return [entry for entry in original_discover() if entry == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.australian_office_of_financial_management_aofm.fetch_text",
            side_effect=[text_result(forthcoming), text_result(data_hub)],
        ), mock.patch(
            "adapters.venues.australian_office_of_financial_management_aofm.fetch_bytes",
            return_value=workbook_result,
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_aofm):
            batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )

        self.assertIn(adapter_id, discover_adapters())
        self.assertIsInstance(get_adapter(adapter_id), AustralianOfficeOfFinancialManagementAofmAdapter)
        self.assertTrue(batch.observations)
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(any(row["source_url"] == AOFM_FORTHCOMING_TRANSACTIONS_URL for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(AOFM_DATA_HUB_URL, get_adapter(adapter_id).info.docs_url)

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
    def test_png_customs_adapter_closes_spec_1042_without_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #1042: Papua New Guinea Customs Service",
            "market_key": "global_discovery|Papua New Guinea Customs Service",
            "spec": {
                "candidate": {
                    "venue_or_source": "Papua New Guinea Customs Service",
                    "public_docs_url": PNG_CUSTOMS_TSC_SOURCE_URL,
                    "asset_or_event": (
                        "Papua New Guinea TSCs for Motor Vehicles: Toyota and other "
                        "model, engine, and year codes mapped to tariff specification codes"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("papua_new_guinea_customs_service", match["adapter_id"])
        self.assertIn("vehicle_tariff_specification_code", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_uae_mof_adapter_closes_spec_674_without_secondary_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #674: Ministry of Finance UAE / Federal Debt Management Office",
            "market_key": "global_discovery|Ministry of Finance UAE / Federal Debt Management Office",
            "spec": {
                "candidate": {
                    "venue_or_source": "Ministry of Finance UAE / Federal Debt Management Office",
                    "public_docs_url": UAE_MOF_ISSUANCE_PROGRAMME_URL,
                    "asset_or_event": (
                        "UAE federal T-Bonds and T-Sukuk issuance programme; issuance table with "
                        "Security Type, ISIN, Maturity Date, and total outstanding domestic debt"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("ministry_of_finance_uae_federal_debt_management_office", match["adapter_id"])
        self.assertIn("outstanding_debt_reference", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_kase_global_adapter_closes_spec_1239_with_reference_quotes_only(self) -> None:
        spec = {
            "title": "Implement public adapter #1239: Kazakhstan Stock Exchange (KASE)",
            "market_key": "global_discovery|Kazakhstan Stock Exchange (KASE)",
            "spec": {
                "candidate": {
                    "venue_or_source": "Kazakhstan Stock Exchange (KASE)",
                    "public_docs_url": KASE_GLOBAL_DOCS_URL,
                    "asset_or_event": (
                        "KASE Global foreign ETFs and ADRs: IBIT_KZ, ETHA_KZ, SOLZ_KZ, "
                        "BITO_KZ, SPY_KZ, QQQ_KZ, BABAd, and BIDUd"
                    ),
                    "why_interesting": "daily quote and settlement reference coverage",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("kazakhstan_stock_exchange_kase_global", match["adapter_id"])
        self.assertIn("delayed_quote", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_cbb_treasury_bill_adapter_closes_spec_679_without_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #679: Central Bank of Bahrain",
            "market_key": "global_discovery|Central Bank of Bahrain",
            "spec": {
                "candidate": {
                    "venue_or_source": "Central Bank of Bahrain",
                    "public_docs_url": CBB_TBILL_SOURCE_URL,
                    "asset_or_event": (
                        "Bahrain Government Treasury Bills weekly auctions "
                        "(issue-numbered, with lowest accepted price and average rate)"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("central_bank_of_bahrain_treasury_bills", match["adapter_id"])
        self.assertIn("event_price_reference", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_eex_uka_adapter_closes_spec_1400_without_quote_claims(self) -> None:
        spec = {
            "title": "Implement public adapter #1400: European Energy Exchange (EEX)",
            "market_key": "global_discovery|European Energy Exchange (EEX)",
            "spec": {
                "candidate": {
                    "venue_or_source": "European Energy Exchange (EEX)",
                    "public_docs_url": EEX_UK_ETS_PAGE_URL,
                    "asset_or_event": "EEX UK Emission Allowance (UKA) Futures and Options December 2026",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("eex_uka_futures_options_public", match["adapter_id"])
        self.assertIn("contract_catalog", match["available_capabilities"])
        self.assertNotIn("ticker", match["available_capabilities"])
        self.assertNotIn("order_book", match["available_capabilities"])

    def test_aib_eex_france_cpb_adapter_closes_spec_1512_with_reporting_price_reference(self) -> None:
        spec = {
            "title": "Implement public adapter #1512: AIB / EEX France",
            "market_key": "global_discovery|AIB / EEX France",
            "spec": {
                "candidate": {
                    "venue_or_source": "AIB / EEX France",
                    "public_docs_url": AIB_EEX_FRANCE_SOURCE_URL,
                    "asset_or_event": (
                        "France biomethane CPBs (certificats de production de biomethane) "
                        "with monthly issued list and monthly average purchase/sale price"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("aib_eex_france_biomethane_cpb_public", match["adapter_id"])
        self.assertIn("monthly_average_transaction_price", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

    def test_casablanca_futures_adapter_closes_spec_956_without_entry_quote_claim(self) -> None:
        spec = {
            "title": "Implement public adapter #956: Casablanca Stock Exchange / Futures market",
            "market_key": "global_discovery|Casablanca Stock Exchange / Futures market",
            "spec": {
                "candidate": {
                    "venue_or_source": "Casablanca Stock Exchange / Futures market",
                    "public_docs_url": CASABLANCA_FUTURES_SOURCE_URL,
                    "asset_or_event": "MASI 20 futures contract FMASI20SEP26",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("casablanca_stock_exchange_futures_market", match["adapter_id"])
        self.assertIn("delayed_quote", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

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

    def test_carb_adapter_closes_spec_1079_with_auction_result_references_only(self) -> None:
        spec = {
            "title": "Implement public adapter #1079: California Air Resources Board",
            "market_key": "global_discovery|California Air Resources Board",
            "spec": {
                "candidate": {
                    "venue_or_source": "California Air Resources Board",
                    "public_docs_url": CARB_AUCTION_INFORMATION_URL,
                    "asset_or_event": (
                        "California Cap-and-Invest joint auction of GHG allowances "
                        "(California/Québec), vintage allowances, and Reserve sales"
                    ),
                    "why_interesting": "auction settlement prices, results, and proceeds publication timing",
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("california_air_resources_board_cap_and_invest", match["adapter_id"])
        self.assertIn("event_price_reference", match["available_capabilities"])
        self.assertNotIn("entry_quality_quote", match["available_capabilities"])

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

    def test_esx_fixed_income_adapter_closes_spec_1296(self) -> None:
        spec = {
            "title": "Implement public adapter #1296: Ethiopian Securities Exchange",
            "market_key": "global_discovery|Ethiopian Securities Exchange",
            "spec": {
                "candidate": {
                    "venue_or_source": "Ethiopian Securities Exchange",
                    "public_docs_url": ESX_FIXED_INCOME_OVERVIEW_URL,
                    "source_urls": [
                        ESX_FIXED_INCOME_INSTRUMENTS_URL,
                        ESX_FIXED_INCOME_OPERATIONS_URL,
                    ],
                    "asset_or_event": (
                        "Government T-Bills, commercial papers, repos, "
                        "Treasury bonds, corporate bonds"
                    ),
                    "data_access_type": "public_no_key",
                }
            },
        }

        match = adapter_capabilities.match_adapter_spec(spec)
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("ethiopian_securities_exchange_fixed_income", match["adapter_id"])
        self.assertIn("fixed_income_instrument_catalog", match["available_capabilities"])
        self.assertNotIn("ticker", match["available_capabilities"])
        self.assertNotIn("order_book", match["available_capabilities"])

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
        self.assertTrue(payload["paper_testable_surface"].startswith("paper:public_adapter:"))
        self.assertTrue(payload["behavioral_gate"])
        self.assertTrue(payload["rollback_criteria"])
        self.assertIn("route_evidence", payload["evidence"])
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

    def test_reconciliation_recovers_failed_acceptance_after_capability_is_added(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.init_db(conn)
        docs_url = "https://example.test/perpetuals"
        storage.add_adapter_spec(
            conn,
            "rec-recovered",
            "global_discovery|Example Perpetuals",
            90,
            "Example perpetuals public market data",
            {
                "candidate": {
                    "venue_or_source": "Example Perpetuals",
                    "public_docs_url": docs_url,
                }
            },
            {},
        )
        conn.execute("update adapter_specs set status = 'deployed_acceptance_failed'")
        conn.commit()
        inventory = [
            {
                "adapter_id": "example_perpetuals",
                "venue": "EXAMPLE_PERPETUALS",
                "source": docs_url,
                "docs_url": docs_url,
                "aliases": ["Example Perpetuals"],
                "capabilities": ["public_market_data", "ticker"],
                "runtime_entrypoint": "adapters.venues.example.ExampleAdapter",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            adapter_capabilities, "capability_inventory", return_value=inventory
        ), mock.patch.object(
            adapter_capabilities, "REPORT_JSON", pathlib.Path(tmp) / "inventory.json"
        ), mock.patch.object(adapter_capabilities, "REPORT_MD", pathlib.Path(tmp) / "inventory.md"):
            adapter_capabilities.reconcile_adapter_specs(conn)

        row = conn.execute("select status, evidence_json from adapter_specs").fetchone()
        evidence = json.loads(row["evidence_json"])
        self.assertEqual("implemented_runtime_adapter", row["status"])
        self.assertEqual("fully_covered", evidence["adapter_capability_reconciliation"]["match_status"])
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
