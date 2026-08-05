"""AIB / EEX France public CPB reporting-surface adapter.

The AIB EEX-France domain protocol documents the public monthly reports for
French *certificats de production de biomethane* (CPBs).  It is a public
reporting-surface reference rather than an anonymous executable quote feed:
the actual issued-certificate list and average transaction price are published
by EEX on a monthly cadence.  This adapter discovers and parses EEX's current
public workbook for paper research and never creates an order route.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = (
    "https://www.aib-net.org/sites/default/files/assets/facts/domain-protocols/"
    "AIB-2026-DPFR-Domain%20Protocol%20EEX%20Gas%20Application%20Final%20Clean%20ESG.pdf"
)
EEX_CPB_PAGE_URL = "https://www.eex.com/en/markets/energy-certificates/french-biogas-production-certificates"
VENUE = "AIB_EEX_FRANCE"
MARKET_SURFACE = "france_biomethane_cpb_monthly_reporting"


class AibEexFranceCpbParseError(ValueError):
    """Raised when the public protocol no longer identifies the CPB reports."""


def extract_pdf_text(body: bytes) -> str:
    """Extract visible text from the bounded, public AIB protocol PDF."""

    if not isinstance(body, bytes) or not body:
        raise AibEexFranceCpbParseError("AIB EEX-France protocol PDF response is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise AibEexFranceCpbParseError(
            "pypdf is required to read the AIB EEX-France protocol PDF"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - revisions must remain source-health evidence.
        raise AibEexFranceCpbParseError(f"AIB EEX-France protocol PDF could not be read: {exc}") from exc
    if not text.strip():
        raise AibEexFranceCpbParseError("AIB EEX-France protocol PDF contains no extractable text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AibEexFranceCpbParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _protocol_date(text: str, fallback: dt.datetime) -> dt.datetime:
    match = re.search(
        r"\bDate\s+(\d{1,2}\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+20\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return fallback
    try:
        return dt.datetime.strptime(match.group(1), "%d %B %Y").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return fallback


def _has(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL))


def parse_aib_eex_france_cpb_reporting(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_days: float = 400.0,
) -> list[dict[str, Any]]:
    """Normalize the protocol's documented French CPB monthly report schema.

    The protocol is intentionally not treated as a monthly price report.  The
    emitted observation instead says exactly which public monthly fields EEX
    documents, preserving a safe foundation for a separately sourced report
    parser when EEX exposes report files without authentication.
    """

    if not isinstance(document, str) or not document.strip():
        raise AibEexFranceCpbParseError("AIB EEX-France protocol text is empty")
    text = " ".join(document.replace("\u00ad", "").replace("\u2010", "-").split())
    if not _has(text, r"\bEEX\s*[-–]\s*FRANCE\b"):
        raise AibEexFranceCpbParseError("EEX-France protocol marker was not found")
    if not _has(text, r"\bNational\s+Biogas\s+Certificates\b"):
        raise AibEexFranceCpbParseError("national-biogas-certificates marker was not found")
    if not _has(text, r"publishes\s+on\s+a\s+monthly\s+basis\s+the\s+list\s+of\s+CPBs?.{0,160}issued"):
        raise AibEexFranceCpbParseError("monthly CPB issued-list publication marker was not found")
    if not _has(text, r"monthly\s+basis\s+the\s+average\s+price.{0,160}(?:purchased|sold)"):
        raise AibEexFranceCpbParseError("monthly CPB average-price publication marker was not found")
    required_fields = {
        "commissioning_date": r"date\s+of\s+commissioning\s+of\s+the\s+installation",
        "issued_biomethane_mwh": r"quantity\s+of\s+biomethane.{0,80}expressed\s+in\s+MWh",
        "injection_period": r"start\s+and\s+end\s+dates\s+of\s+the\s+injection\s+period",
        "certificate_issuance_date": r"date\s+of\s+issuance\s+of\s+the\s+certificate",
    }
    missing = [field for field, pattern in required_fields.items() if not _has(text, pattern)]
    if missing:
        raise AibEexFranceCpbParseError(
            "monthly CPB issued-list field markers were not found: " + ", ".join(missing)
        )

    fetched_at = _received_time(received_at)
    protocol_date = _protocol_date(text, fetched_at)
    age_seconds = max(0.0, (fetched_at - protocol_date).total_seconds())
    freshness_state = (
        "fresh" if age_seconds <= max(0.0, float(stale_after_days)) * 86400.0 else "stale"
    )
    return [
        {
            "venue": VENUE,
            "inst_id": f"{VENUE}:CPB:MONTHLY_REPORTING",
            "instrument_id": f"{VENUE}:CPB:MONTHLY_REPORTING",
            "symbol": "FR_CPB_MONTHLY",
            "name": "France biomethane CPB monthly issuance and average-price reporting",
            "base": "FRANCE_BIOMETHANE_CPB",
            "quote": "EUR_PER_CPB",
            "market_type": "national_biogas_certificate_reporting_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "renewable_gas_certificate",
            "trade_type": "official_reporting_surface_reference",
            "direction": "watch_only",
            "last": 0.0,
            "monthly_issued_list_available": True,
            "monthly_average_purchase_sale_price_available": True,
            "issued_list_fields": sorted(required_fields),
            "reporting_frequency": "monthly",
            "reported_price": None,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_cpb_monthly_reporting_protocol",
            "freshness_state": freshness_state,
            "freshness_basis": "aib_eex_france_protocol_publication_date",
            "freshness_age_seconds": round(age_seconds, 3),
            "session_status": "monthly_reporting_reference",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "protocol_date": protocol_date.date().isoformat(),
            "price_source": "EEX France CPB monthly average purchase/sale price publication",
            "source_url": source_url,
            "report_data_status": "protocol_confirms_public_monthly_reports_not_embedded_report_values",
        }
    ]


# Short compatibility aliases for report-oriented callers.
parse_aib_eex_france_cpbs = parse_aib_eex_france_cpb_reporting
parse_aib_eex_france_cpb_protocol = parse_aib_eex_france_cpb_reporting


def _field_token(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower().replace(" ", "").replace("'", "")


def discover_cpb_workbook_url(document: str, *, source_url: str = EEX_CPB_PAGE_URL) -> str:
    """Find EEX's current public CPB issuance-and-price workbook on its page."""

    if not isinstance(document, str) or not document.strip():
        raise AibEexFranceCpbParseError("EEX CPB publication page is empty")
    links = re.findall(r"(?:href|data-download-url)\s*=\s*[\"']([^\"']+\.xlsx(?:\?[^\"']*)?)[\"']", document, re.IGNORECASE)
    candidates = [html.unescape(link).strip() for link in links]
    candidates = [link for link in candidates if "cpb" in link.lower() or "biogas" in link.lower()]
    if not candidates:
        raise AibEexFranceCpbParseError("EEX CPB XLSX publication link was not found")
    return urllib.parse.urljoin(source_url, candidates[0])


def _xlsx_sheets(content: bytes) -> list[list[dict[int, Any]]]:
    if not isinstance(content, bytes) or not content:
        raise AibEexFranceCpbParseError("EEX CPB XLSX response is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as exc:
        raise AibEexFranceCpbParseError(f"invalid EEX CPB XLSX: {exc}") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > 25_000_000:
            raise AibEexFranceCpbParseError("expanded EEX CPB XLSX content exceeds 25000000 byte limit")
        worksheet_names = sorted(
            name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        if not worksheet_names:
            raise AibEexFranceCpbParseError("EEX CPB XLSX worksheet was not found")
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise AibEexFranceCpbParseError(f"invalid EEX CPB XLSX shared strings: {exc}") from exc
            shared = ["".join(node.text or "" for node in item.iter(f"{namespace}t")) for item in root.findall(f"{namespace}si")]
        sheets: list[list[dict[int, Any]]] = []
        for worksheet_name in worksheet_names:
            try:
                sheet = ET.fromstring(archive.read(worksheet_name))
            except ET.ParseError as exc:
                raise AibEexFranceCpbParseError(f"invalid EEX CPB XLSX worksheet: {exc}") from exc
            rows: list[dict[int, Any]] = []
            for row_node in sheet.iter(f"{namespace}row"):
                row: dict[int, Any] = {}
                for cell in row_node.findall(f"{namespace}c"):
                    match = re.match(r"([A-Z]+)", str(cell.get("r") or ""))
                    if not match:
                        continue
                    column = 0
                    for char in match.group(1):
                        column = column * 26 + ord(char) - 64
                    raw_node = cell.find(f"{namespace}v")
                    raw = raw_node.text if raw_node is not None else None
                    cell_type = cell.get("t")
                    if cell_type == "s" and raw is not None:
                        try:
                            value: Any = shared[int(raw)]
                        except (IndexError, ValueError) as exc:
                            raise AibEexFranceCpbParseError("EEX CPB XLSX shared string index is invalid") from exc
                    elif cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{namespace}t"))
                    elif raw is None:
                        value = None
                    else:
                        try:
                            value = float(raw)
                        except ValueError:
                            value = raw
                    row[column] = value
                if row:
                    rows.append(row)
            sheets.append(rows)
    return sheets


def _excel_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            return dt.date(1899, 12, 30) + dt.timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(str(value).strip(), pattern).date()
        except ValueError:
            continue
    return None


_FRENCH_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}


def _month_period(value: Any) -> tuple[int, int] | None:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    year = re.search(r"\b(20\d{2})\b", text)
    if not year:
        return None
    for name, month in _FRENCH_MONTHS.items():
        if re.search(rf"\b{name}\b", text):
            return int(year.group(1)), month
    return None


def _freshness(event_date: dt.date, fetched_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    age = max(0.0, (fetched_at.date() - event_date).total_seconds())
    return ("fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale", round(age, 3))


def parse_aib_eex_france_cpb_workbook(
    content: bytes,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 93.0,
) -> list[dict[str, Any]]:
    """Normalize EEX's public CPB price table and issued-certificate list XLSX."""

    fetched_at = _received_time(received_at)
    sheets = _xlsx_sheets(content)
    price_rows: list[dict[str, Any]] = []
    issued_rows: list[dict[str, Any]] = []
    for sheet in sheets:
        for position, raw_header in enumerate(sheet):
            headers = {column: str(value or "").strip() for column, value in raw_header.items()}
            tokens = {_field_token(value) for value in headers.values()}
            if "moisdepublication" in tokens and any(token.startswith("certificats") for token in tokens):
                for raw_row in sheet[position + 1 :]:
                    period = _month_period(raw_row.get(next((column for column, value in headers.items() if _field_token(value) == "moisdepublication"), 0)))
                    if period is None:
                        continue
                    for column, header in headers.items():
                        year_match = re.search(r"\b(20\d{2})\b", header)
                        price = number(raw_row.get(column))
                        if not year_match or price is None or price <= 0:
                            continue
                        year, month = period
                        event_date = dt.date(year, month, 1)
                        freshness_state, freshness_age = _freshness(event_date, fetched_at, stale_after_days)
                        vintage_year = int(year_match.group(1))
                        price_rows.append(
                            {
                                "venue": VENUE,
                                "inst_id": f"{VENUE}:CPB:AVERAGE_PRICE:{vintage_year}:{year:04d}-{month:02d}",
                                "instrument_id": f"{VENUE}:CPB:AVERAGE_PRICE:{vintage_year}:{year:04d}-{month:02d}",
                                "symbol": "FR_CPB_AVERAGE_PRICE",
                                "name": "France biomethane CPB monthly average purchase/sale price",
                                "base": "FRANCE_BIOMETHANE_CPB",
                                "quote": "EUR_PER_CPB",
                                "market_type": "national_biogas_certificate_monthly_price_reference",
                                "market_surface": MARKET_SURFACE,
                                "asset_class": "renewable_gas_certificate",
                                "trade_type": "official_monthly_average_transaction_price",
                                "direction": "watch_only",
                                "last": price,
                                "monthly_average_purchase_sale_price_eur": price,
                                "certificate_issuance_year": vintage_year,
                                "reporting_month": f"{year:04d}-{month:02d}",
                                "data_status": "reachable",
                                "fetch_status": "reachable",
                                "quality_status": "official_monthly_average_transaction_price",
                                "freshness_state": freshness_state,
                                "freshness_basis": "eex_public_cpb_reporting_month",
                                "freshness_age_seconds": freshness_age,
                                "session_status": "monthly_reported",
                                "observed_at": fetched_at.isoformat(),
                                "fetched_at": fetched_at.isoformat(),
                                "price_source": "EEX France CPB issuance-data and price workbook",
                                "source_url": source_url,
                                "source_page_url": EEX_CPB_PAGE_URL,
                                "source_protocol_url": SOURCE_URL,
                                "candidate_reject_reason": "monthly_cpb_price_reference_not_executable_quote",
                            }
                        )
            if {"datedemission", "volumedescertificats", "volumeenmwh"}.issubset(tokens):
                fields = {_field_token(value): column for column, value in headers.items()}
                for index, raw_row in enumerate(sheet[position + 1 :], start=1):
                    issuance_date = _excel_date(raw_row.get(fields["datedemission"]))
                    volume_cpb = number(raw_row.get(fields["volumedescertificats"]))
                    volume_mwh = number(raw_row.get(fields["volumeenmwh"]))
                    if issuance_date is None or volume_cpb is None or volume_mwh is None:
                        continue
                    freshness_state, freshness_age = _freshness(issuance_date, fetched_at, stale_after_days)
                    issued_rows.append(
                        {
                            "venue": VENUE,
                            "inst_id": f"{VENUE}:CPB:ISSUANCE:{issuance_date.isoformat()}:{index}",
                            "instrument_id": f"{VENUE}:CPB:ISSUANCE:{issuance_date.isoformat()}:{index}",
                            "symbol": "FR_CPB_ISSUANCE",
                            "name": "France biomethane CPB issued certificate batch",
                            "base": "FRANCE_BIOMETHANE_CPB",
                            "quote": "EUR_PER_CPB",
                            "market_type": "national_biogas_certificate_issuance_reference",
                            "market_surface": MARKET_SURFACE,
                            "asset_class": "renewable_gas_certificate",
                            "trade_type": "official_monthly_issued_certificate_list",
                            "direction": "watch_only",
                            "last": 0.0,
                            "issued_cpb_volume": volume_cpb,
                            "issued_biomethane_mwh": volume_mwh,
                            "issuance_date": issuance_date.isoformat(),
                            "production_start_date": _excel_date(raw_row.get(fields.get("datededebutdeproduction"))),
                            "production_end_date": _excel_date(raw_row.get(fields.get("datedefindeproduction"))),
                            "installation_commissioning_date": _excel_date(raw_row.get(fields.get("datedemiseenservice"))),
                            "data_status": "reachable",
                            "fetch_status": "reachable",
                            "quality_status": "official_monthly_issued_certificate_list",
                            "freshness_state": freshness_state,
                            "freshness_basis": "official_cpb_issuance_date",
                            "freshness_age_seconds": freshness_age,
                            "session_status": "monthly_reported",
                            "observed_at": fetched_at.isoformat(),
                            "fetched_at": fetched_at.isoformat(),
                            "source_url": source_url,
                            "source_page_url": EEX_CPB_PAGE_URL,
                            "source_protocol_url": SOURCE_URL,
                            "candidate_reject_reason": "issued_cpb_list_reference_not_executable_quote",
                        }
                    )
    if not price_rows or not issued_rows:
        missing = []
        if not price_rows:
            missing.append("monthly average price rows")
        if not issued_rows:
            missing.append("issued CPB list rows")
        raise AibEexFranceCpbParseError("EEX CPB XLSX has no usable " + " and ".join(missing))
    return [*price_rows, *issued_rows]


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "content_type": str(result.get("content_type") or "") or None,
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(
    result: dict[str, Any], source_url: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:CPB:MONTHLY_REPORTING:HEALTH",
            "instrument_id": f"{VENUE}:CPB:MONTHLY_REPORTING:HEALTH",
            "symbol": "FR_CPB_MONTHLY",
            "base": "FRANCE_BIOMETHANE_CPB",
            "quote": "EUR_PER_CPB",
            "market_type": "national_biogas_certificate_reporting_reference",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_cpb_reporting_protocol_parser_failure"
                if parser_error
                else "public_cpb_reporting_protocol_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class AibEexFranceBiomethaneCpbAdapter:
    info = AdapterInfo(
        adapter_id="aib_eex_france_biomethane_cpb_public",
        venue=VENUE,
        market_type="national_biogas_certificate_reporting_reference",
        source="AIB / EEX France public biomethane CPB monthly reporting workbooks",
        capabilities=(
            "public_market_data",
            "national_biogas_certificates",
            "biomethane",
            "monthly_issued_certificate_list",
            "monthly_average_transaction_price",
            "event_price_reference",
            "source_health",
        ),
        aliases=(
            "aib",
            "aib eex france",
            "eex france",
            "france biomethane cpb",
            "certificats de production de biomethane",
            "national biogas certificates",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.aib_eex_france.AibEexFranceBiomethaneCpbAdapter"
        ),
        quote_assets=("EUR_PER_CPB",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        page_url = str(cfg.get("source_page_url") or EEX_CPB_PAGE_URL)
        page_result = fetch_text(page_url, timeout)
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {
            "cpb_publication_page": _fetch_evidence(page_result, page_url)
        }
        if not page_result.get("ok"):
            observations = [_failure_observation(page_result, page_url)]
            source_status = str(page_result.get("status") or "unavailable")
        else:
            try:
                workbook_url = str(cfg.get("workbook_url") or discover_cpb_workbook_url(
                    str(page_result.get("text") or ""), source_url=page_url
                ))
            except (AibEexFranceCpbParseError, TypeError, ValueError) as exc:
                message = f"AIB / EEX France CPB publication-page parser failed: {exc}"[:300]
                parser_failures.append({"source_url": page_url, "error": message})
                observations = [_failure_observation(page_result, page_url, message)]
                source_status = "degraded"
            else:
                workbook_result = fetch_bytes(workbook_url, timeout)
                fetch_status["cpb_issuance_price_workbook"] = _fetch_evidence(workbook_result, workbook_url)
                if not workbook_result.get("ok"):
                    observations = [_failure_observation(workbook_result, workbook_url)]
                    source_status = str(workbook_result.get("status") or "unavailable")
                else:
                    try:
                        observations = parse_aib_eex_france_cpb_workbook(
                            workbook_result.get("content") or b"",
                            source_url=workbook_url,
                            received_at=workbook_result.get("received_at"),
                            stale_after_days=max(0.0, float(cfg.get("stale_after_days", 93.0))),
                        )
                        source_status = "reachable"
                    except (AibEexFranceCpbParseError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                        message = f"AIB / EEX France CPB workbook parser failed: {exc}"[:300]
                        parser_failures.append({"source_url": workbook_url, "error": message})
                        observations = [_failure_observation(workbook_result, workbook_url, message)]
                        source_status = "degraded"

        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1512,
                "source_status": source_status,
                "source_url": page_url,
                "source_urls": [
                    page_url,
                    SOURCE_URL,
                    *[
                        item["source_url"]
                        for item in fetch_status.values()
                        if item["source_url"] != page_url
                    ],
                ],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "capability_gap": "public_entry_quality_quote_and_order_book_not_available",
                "paper_only": True,
            },
        )


register_adapter(AibEexFranceBiomethaneCpbAdapter())
