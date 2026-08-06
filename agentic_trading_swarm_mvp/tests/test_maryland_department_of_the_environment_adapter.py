from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.maryland_department_of_the_environment import (
    MARKET_BOARD_URL,
    PROGRAM_URL,
    PURCHASING_FAQ_URL,
    REGISTRY_WORKBOOK_URL,
    MarylandDepartmentOfTheEnvironmentAdapter,
    parse_maryland_wqt_market_board,
    parse_maryland_wqt_market_pricing,
    parse_maryland_wqt_registry_workbook,
)


def _pricing_fixture() -> str:
    return """
    <html><body>
      <table>
        <tr><th>WQT Market Pricing</th><th>Nitrogen ($/lb)</th><th>Phosphorus ($/lb)</th><th>Sediment ($/lb)*</th></tr>
        <tr><td>Low</td><td>$45.80</td><td>$58.30</td><td>$0.09</td></tr>
        <tr><td>Middle</td><td>$125.98</td><td>$263.27</td><td>$0.59</td></tr>
        <tr><td>High</td><td>$237.50</td><td>$644.50</td><td>$1.10</td></tr>
      </table>
    </body></html>
    """


def _market_board_fixture() -> str:
    return """
    <html><body>
      <table class="ui-responsive table-stripe">
        <thead>
          <tr>
            <th>Ad Type</th>
            <th>Contact Info</th>
            <th>Segmentshed</th>
            <th>Credit-Year-Needed</th>
            <th>Credits Needed/Available</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>For Sale</td>
            <td>Doug Abbott<br>Easton Utilities<br>Superintendent of Operations<br>410-763-9426<br>[email protected]</td>
            <td>CHOOH</td>
            <td>2018</td>
            <td>TN Credits:330<br>TP Credits:50<br>TSS Credits:6,470</td>
          </tr>
          <tr>
            <td>Wanted</td>
            <td>Alex Buyer<br>Example County MS4<br>Stormwater Manager<br>410-555-0000<br>[email protected]</td>
            <td>POTTF_MD, ANATF_DC</td>
            <td>2026</td>
            <td>TN Credits:12<br>TP Credits:3<br>TSS Credits:900</td>
          </tr>
        </tbody>
      </table>
    </body></html>
    """


def _sheet_xml(rows: list[list[tuple[str, object]]]) -> str:
    def inline_cell(ref: str, value: object) -> str:
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'

    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(inline_cell(f"{column}{row_index}", value) for column, value in row)
        row_xml.append(f'<row r="{row_index}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    )


def _registry_workbook_fixture() -> bytes:
    readme = _sheet_xml(
        [
            [("A", "MDE's Water Quality Trading Register")],
            [("A", "Read me")],
        ]
    )
    generated = _sheet_xml(
        [
            [
                ("A", "Credit Status"),
                ("B", "Credits Remaining"),
                ("C", "Current Credit IDs"),
                ("D", "Generator"),
                ("E", "Credit Sector"),
                ("F", "Contact"),
                ("G", "Contact Address"),
                ("H", "Telephone"),
                ("I", "Email"),
                ("J", "Watershed"),
                ("K", "Vintage"),
                ("L", "Credit Type"),
                ("M", "Total Credits Certified"),
                ("N", "Date Certified"),
            ],
            [
                ("A", "Available"),
                ("B", 6648),
                ("C", "2018_CHOOH_N_00001 | 06648"),
                ("D", "Easton Utilities"),
                ("E", "WWTP"),
                ("F", "Doug Abbott"),
                ("G", "201 N. Washington St., Easton, MD"),
                ("H", "410-763-9426"),
                ("I", "[email protected]"),
                ("J", "CHOOH"),
                ("K", 2018),
                ("L", "Nitrogen"),
                ("M", 6648),
                ("N", 43509),
            ],
            [
                ("A", "Traded"),
                ("B", 0),
                ("C", "2018_PATMH_P_000001"),
                ("D", "Maryland Port Administration"),
                ("E", "SW/ALT"),
                ("F", "William Richardson"),
                ("G", "2700 Broening Hwy., Baltimore, MD 21222"),
                ("H", "410-633-1145"),
                ("I", "[email protected]"),
                ("J", "PATMH"),
                ("K", 2018),
                ("L", "Phosphorus"),
                ("M", 1),
                ("N", 43517),
            ],
        ]
    )
    reserve = _sheet_xml(
        [
            [
                ("A", "Credit IDs"),
                ("B", "Generator"),
                ("C", "County"),
                ("D", "Watershed"),
                ("E", "Vintage"),
                ("F", "Credit Type"),
                ("G", "# Credits"),
                ("H", "Date Certified"),
                ("I", "Credit Status"),
            ],
            [
                ("A", "2018_CHOOH_S_129361 | 136168"),
                ("B", "Easton Utilities"),
                ("C", "Talbot"),
                ("D", "CHOOH"),
                ("E", 2018),
                ("F", "Sediment"),
                ("G", 6808),
                ("H", 43509),
                ("I", "Certified"),
            ],
        ]
    )
    trades = _sheet_xml(
        [
            [
                ("A", "Credit IDs"),
                ("B", "Generator"),
                ("C", "Owner"),
                ("D", "Watershed"),
                ("E", "Vintage"),
                ("F", "Credit Type"),
                ("G", "# Credits"),
                ("H", "New Owner"),
                ("I", "Credits Acquired"),
                ("J", "Applied to Permit"),
                ("K", "Permit #"),
                ("L", "Date Registered"),
            ],
            [
                ("A", "2018_ELKOH_N_000001 | 000030"),
                ("B", "Elkton Wastewater Treatment Plant"),
                ("C", "Town of Elkton"),
                ("D", "ELKOH"),
                ("E", 2018),
                ("F", "Nitrogen"),
                ("G", 30),
                ("H", "Terumo Medical Corporation"),
                ("I", 43525),
                ("J", "Yes"),
                ("K", "12SR0433"),
                ("L", 43525),
            ],
        ]
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        '<sheet name="ReadMe" sheetId="1" r:id="rId1"/>'
        '<sheet name="Credits_Generated" sheetId="2" r:id="rId2"/>'
        '<sheet name="MD_Reserve" sheetId="3" r:id="rId3"/>'
        '<sheet name="All_Trades" sheetId="4" r:id="rId4"/>'
        '</sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/>'
        '</Relationships>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", readme)
        archive.writestr("xl/worksheets/sheet2.xml", generated)
        archive.writestr("xl/worksheets/sheet3.xml", reserve)
        archive.writestr("xl/worksheets/sheet4.xml", trades)
    return output.getvalue()


def _text_result(text: str) -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": "2026-08-05T12:00:00+00:00",
        "latency_ms": 2.0,
    }


def _bytes_result(content: bytes) -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "content": content,
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "received_at": "2026-08-05T12:00:00+00:00",
        "latency_ms": 3.0,
    }


class MarylandDepartmentOfTheEnvironmentAdapterTests(unittest.TestCase):
    def test_parsers_normalize_pricing_board_and_registry_surfaces(self) -> None:
        pricing_rows = parse_maryland_wqt_market_pricing(
            _pricing_fixture(), received_at="2026-08-05T12:00:00+00:00"
        )
        self.assertEqual(3, len(pricing_rows))
        nitrogen = next(row for row in pricing_rows if row["pollutant"] == "Nitrogen")
        self.assertEqual(45.8, nitrogen["price_low_usd_per_lb"])
        self.assertEqual(125.98, nitrogen["last"])
        self.assertEqual("reference_only", nitrogen["session_status"])

        board_rows = parse_maryland_wqt_market_board(
            _market_board_fixture(), received_at="2026-08-05T12:00:00+00:00"
        )
        self.assertEqual(6, len(board_rows))
        for_sale_n = next(
            row
            for row in board_rows
            if row["listing_side"] == "for_sale" and row["pollutant"] == "Nitrogen"
        )
        self.assertEqual(330.0, for_sale_n["credits_listed_or_needed"])
        self.assertEqual("Easton Utilities", for_sale_n["listing_entity"])
        self.assertTrue(for_sale_n["direct_contact_channels_omitted"])
        self.assertNotIn("listing_email", for_sale_n)

        wanted_s = next(
            row
            for row in board_rows
            if row["listing_side"] == "wanted" and row["pollutant"] == "Sediment"
        )
        self.assertEqual(["POTTF_MD", "ANATF_DC"], wanted_s["segmentsheds"])
        self.assertEqual("buyer_interest_active", wanted_s["session_status"])

        registry_rows = parse_maryland_wqt_registry_workbook(
            _registry_workbook_fixture(), received_at="2026-08-05T12:00:00+00:00"
        )
        self.assertEqual(4, len(registry_rows))
        generated = next(row for row in registry_rows if row["market_surface"].endswith("credits_generated"))
        self.assertEqual(6648.0, generated["credits_remaining"])
        self.assertEqual("registry_available", generated["session_status"])
        self.assertEqual("2019-02-13", generated["date_certified"])

        reserve = next(row for row in registry_rows if row["market_surface"].endswith("reserve_pool"))
        self.assertEqual(6808.0, reserve["reserve_credits"])
        self.assertEqual("Talbot", reserve["county"])

        trade = next(row for row in registry_rows if row["market_surface"].endswith("registered_trades"))
        self.assertEqual(30.0, trade["traded_credits"])
        self.assertEqual("2019-03-01", trade["registered_date"])
        self.assertIsNone(trade["reported_credits_acquired_value"])
        self.assertEqual("12SR0433", trade["permit_number"])

    def test_scan_preserves_degraded_and_unavailable_sources_as_watch_only_evidence(self) -> None:
        adapter = MarylandDepartmentOfTheEnvironmentAdapter()

        def good_text(url: str, _timeout: int, *, method: str = "GET", json_body=None) -> dict:
            if url == PROGRAM_URL:
                return _text_result(_pricing_fixture())
            if url == MARKET_BOARD_URL:
                return _text_result(_market_board_fixture())
            raise AssertionError(url)

        with mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_text",
            side_effect=good_text,
        ), mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_bytes",
            return_value=_bytes_result(_registry_workbook_fixture()),
        ):
            batch = adapter.scan({})
        self.assertEqual("reachable", batch.metadata["source_status"])
        self.assertEqual(1255, batch.metadata["adapter_spec_id"])
        self.assertEqual([], batch.metadata["parser_failures"])
        self.assertTrue(batch.metadata["paper_only"])
        self.assertEqual(3, batch.metadata["pricing_observation_count"])
        self.assertEqual(6, batch.metadata["market_board_observation_count"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["registry_workbook"]["fetch_status"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))

        blocked = {
            "ok": False,
            "status": "blocked",
            "http_status": 403,
            "text": "",
            "received_at": "2026-08-05T12:00:00+00:00",
            "latency_ms": 5.0,
            "error": "HTTP Error 403",
        }
        malformed_workbook = {
            "ok": True,
            "status": "reachable",
            "http_status": 200,
            "content": b"not-a-zip",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "received_at": "2026-08-05T12:00:00+00:00",
            "latency_ms": 5.0,
        }

        def mixed_text(url: str, _timeout: int, *, method: str = "GET", json_body=None) -> dict:
            if url == PROGRAM_URL:
                return _text_result("<html><body>missing pricing table</body></html>")
            if url == MARKET_BOARD_URL:
                return blocked
            raise AssertionError(url)

        with mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_text",
            side_effect=mixed_text,
        ), mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_bytes",
            return_value=malformed_workbook,
        ):
            failed = adapter.scan({})
        self.assertEqual("degraded", failed.metadata["source_status"])
        self.assertEqual("blocked", failed.metadata["fetch_status"]["market_board"]["fetch_status"])
        self.assertEqual(2, len(failed.metadata["parser_failures"]))
        self.assertTrue(all(row["direction"] == "watch_only" for row in failed.observations))
        self.assertTrue(any(row.get("parser_failure") for row in failed.observations))
        self.assertTrue(
            any(
                row.get("market_surface") == "maryland_water_quality_trading_market_board"
                and row.get("fetch_status") == "blocked"
                for row in failed.observations
            )
        )

    def test_plugin_is_runtime_discoverable_and_builds_runtime_batch(self) -> None:
        adapter_id = "maryland_department_of_the_environment"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, MarylandDepartmentOfTheEnvironmentAdapter)
        self.assertEqual(PROGRAM_URL, adapter.info.docs_url)
        self.assertIn("nutrient_credit_registry", adapter.info.capabilities)

        def fake_text(url: str, _timeout: int, *, method: str = "GET", json_body=None) -> dict:
            return _text_result(_pricing_fixture() if url == PROGRAM_URL else _market_board_fixture())

        original_discover = adapter_runtime.discover_adapters

        def discover_only_maryland() -> list[str]:
            return [item for item in original_discover() if item == adapter_id]

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_text",
            side_effect=fake_text,
        ), mock.patch(
            "adapters.venues.maryland_department_of_the_environment.fetch_bytes",
            return_value=_bytes_result(_registry_workbook_fixture()),
        ), mock.patch.object(adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(adapter_runtime, "discover_adapters", side_effect=discover_only_maryland):
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
        self.assertEqual(13, len(batch.observations))
        self.assertTrue(all(row["venue"] == "MDE_MARYLAND_WQT" for row in batch.observations))
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])

    def test_spec_1255_is_covered_by_the_runtime_adapter(self) -> None:
        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #1255: Maryland Department of the Environment",
                "market_key": "global_discovery|Maryland Department of the Environment",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Maryland Department of the Environment",
                        "public_docs_url": PROGRAM_URL,
                        "source_urls": [MARKET_BOARD_URL, PURCHASING_FAQ_URL],
                        "asset_or_event": (
                            "Maryland Water Quality Trading certified credits and market board "
                            "for nitrogen, phosphorus, and sediment reductions"
                        ),
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual("maryland_department_of_the_environment", match["adapter_id"])
        self.assertIn("nutrient_credit_registry", match["available_capabilities"])


if __name__ == "__main__":
    unittest.main()
