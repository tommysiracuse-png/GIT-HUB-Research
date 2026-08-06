from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import adapter_capabilities
import adapter_runtime
from adapters.registry import discover_adapters, get_adapter
from adapters.venues.namibia_securities_exchange import (
    MARKET_INFO_URL,
    NamibiaSecuritiesExchangeAdapter,
    discover_latest_report_url,
    parse_nsx_weekly_report,
)


PAGE_HTML = """
<html><body>
  <a href="https://nsx.com.na/wp-content/uploads/shared-files/nsx-reports/NSX-Weekly.2026.07.1726.29.xlsx">
    Weekly Trading Report 13 Jul-17 Jul '26
  </a>
</body></html>
"""


def text_result(text: str, received_at: str = "2026-07-18T08:30:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "text": text,
        "received_at": received_at,
        "latency_ms": 4.0,
    }


def bytes_result(content: bytes, received_at: str = "2026-07-18T08:31:00+00:00") -> dict:
    return {
        "ok": True,
        "status": "reachable",
        "http_status": 200,
        "content": content,
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "received_at": received_at,
        "latency_ms": 8.0,
    }


def build_nsx_weekly_workbook() -> bytes:
    rows = [
        ["", "", "10 July - 17 July 2026"],
        [],
        ["", "", "", "Share Code", "ISIN", "Notices", "", "NSX end of day Price", "NSX Previous Close", "", "", "Traded Volume", "Day's total Value", "# Deals", "Day High", "Day Low", "Bid", "Ask", "Total # of shares in issue", "Market Cap at close"],
        ["", "", "Satrix MSCI World Feeder NM", "SXNWDM", "ZAE000246104", "A", "", 117.31, 116.79, "", "", 0, 0, 0, 122.85, 116.54, 0, 0, 210804039, 24729421815.09],
        ["", "", "Satrix S&P 500 Feeder NM", "SXN500", "ZAE000246641", "A", "", 132.17, 131.54, "", "", 0, 0, 0, 132.70, 131.10, 0, 0, 91874051, 12142993320.67],
    ]

    def col_label(index: int) -> str:
        label = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            label = chr(65 + remainder) + label
        return label

    def cell_xml(ref: str, value: object) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'

    row_xml = []
    for row_index, values in enumerate(rows, start=1):
        cells = "".join(cell_xml(f"{col_label(column_index)}{row_index}", value) for column_index, value in enumerate(values, start=1))
        row_xml.append(f'<row r="{row_index}">{cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="ETP &amp; DevX" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


class NamibiaSecuritiesExchangeAdapterTests(unittest.TestCase):
    def test_discover_latest_report_url_uses_public_weekly_xlsx(self) -> None:
        self.assertEqual(
            "https://nsx.com.na/wp-content/uploads/shared-files/nsx-reports/NSX-Weekly.2026.07.1726.29.xlsx",
            discover_latest_report_url(PAGE_HTML),
        )

    def test_parser_normalizes_sxn500_and_sxnwdm_with_public_weekly_fields(self) -> None:
        rows = parse_nsx_weekly_report(
            build_nsx_weekly_workbook(),
            source_url="https://nsx.com.na/wp-content/uploads/shared-files/nsx-reports/NSX-Weekly.2026.07.1726.29.xlsx",
            received_at="2026-07-18T08:31:00+00:00",
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual({"SXN500", "SXNWDM"}, set(by_symbol))
        self.assertEqual(132.17, by_symbol["SXN500"]["last"])
        self.assertEqual(131.54, by_symbol["SXN500"]["previous_close"])
        self.assertEqual("ZAE000246641", by_symbol["SXN500"]["isin"])
        self.assertEqual(91874051, by_symbol["SXN500"]["shares_in_issue"])
        self.assertEqual(117.31, by_symbol["SXNWDM"]["last"])
        self.assertEqual("fresh", by_symbol["SXNWDM"]["freshness_state"])
        self.assertEqual("weekly_report_reference", by_symbol["SXNWDM"]["session_status"])
        self.assertEqual("watch_only", by_symbol["SXNWDM"]["direction"])
        self.assertTrue(by_symbol["SXNWDM"]["paper_experiment_eligible"])
        self.assertNotIn("candidate_reject_reason", by_symbol["SXNWDM"])

    def test_adapter_runtime_discovery_emits_real_weekly_observations(self) -> None:
        adapter_id = "namibia_securities_exchange_nsx_satrix_feeders"
        self.assertIn(adapter_id, discover_adapters())
        adapter = get_adapter(adapter_id)
        self.assertIsInstance(adapter, NamibiaSecuritiesExchangeAdapter)
        self.assertEqual(MARKET_INFO_URL, adapter.info.docs_url)
        self.assertIn("delayed_quote", adapter.info.capabilities)

        original_discover = adapter_runtime.discover_adapters

        def discover_only_nsx() -> list[str]:
            return [candidate for candidate in original_discover() if candidate == adapter_id]

        workbook = build_nsx_weekly_workbook()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "adapters.venues.namibia_securities_exchange.fetch_text",
            return_value=text_result(PAGE_HTML),
        ), mock.patch(
            "adapters.venues.namibia_securities_exchange.fetch_bytes",
            return_value=bytes_result(workbook),
        ), mock.patch.object(
            adapter_runtime, "RUNS_DIR", pathlib.Path(tmp)
        ), mock.patch.object(
            adapter_runtime, "CACHE_DIR", pathlib.Path(tmp) / "cache"
        ), mock.patch.object(
            adapter_runtime, "REPORT_JSON", pathlib.Path(tmp) / "report.json"
        ), mock.patch.object(
            adapter_runtime, "REPORT_MD", pathlib.Path(tmp) / "report.md"
        ), mock.patch.object(
            adapter_runtime, "discover_adapters", side_effect=discover_only_nsx
        ):
            batch = adapter_runtime.build_scan_batch(
                {"public_market_adapters": {"enabled": True, "workers": 1, "adapters": {adapter_id: {"cache_minutes": 0}}}}
            )

        self.assertEqual([], batch.candidates)
        self.assertEqual(2, len(batch.observations))
        self.assertEqual({"SXN500", "SXNWDM"}, {row["symbol"] for row in batch.observations})
        report = batch.metadata["public_market_adapters"]
        self.assertEqual(adapter_id, report["adapters"][0]["adapter_id"])
        self.assertEqual("reachable", report["adapters"][0]["source_status"])
        self.assertEqual(2, report["adapters"][0]["price_observation_count"])

        match = adapter_capabilities.match_adapter_spec(
            {
                "title": "Implement public adapter #364: Namibia Securities Exchange",
                "market_key": "global_discovery|Namibia Securities Exchange",
                "spec": {
                    "candidate": {
                        "venue_or_source": "Namibia Securities Exchange",
                        "public_docs_url": MARKET_INFO_URL,
                        "data_access_type": "public_no_key",
                    }
                },
            }
        )
        self.assertEqual("fully_covered", match["match_status"])
        self.assertEqual(adapter_id, match["adapter_id"])

    def test_adapter_preserves_parser_failure_as_watch_only_surface_evidence(self) -> None:
        with mock.patch(
            "adapters.venues.namibia_securities_exchange.fetch_text",
            return_value=text_result(PAGE_HTML),
        ), mock.patch(
            "adapters.venues.namibia_securities_exchange.fetch_bytes",
            return_value=bytes_result(b"not a workbook"),
        ):
            batch = NamibiaSecuritiesExchangeAdapter().scan({})

        self.assertEqual([], batch.candidates)
        self.assertEqual("degraded", batch.metadata["source_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["market_info_page"]["fetch_status"])
        self.assertEqual("reachable", batch.metadata["fetch_status"]["weekly_report"]["fetch_status"])
        self.assertEqual(1, len(batch.metadata["parser_failures"]))
        self.assertEqual(0, batch.metadata["real_observation_count"])
        self.assertTrue(all(row["direction"] == "watch_only" for row in batch.observations))
        self.assertTrue(all(row["candidate_reject_reason"] == "public_nsx_weekly_report_parser_failure" for row in batch.observations))


if __name__ == "__main__":
    unittest.main()
