"""Australian Office of Financial Management Treasury Bond tender adapter.

The AOFM publishes forthcoming Australian Treasury Bond tenders as HTML and
publishes the tender history in a public Data Hub workbook.  Neither source is
an executable secondary-market quote, so this adapter supplies paper-only,
watch-only auction references and source-health evidence.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, html_tables, slug, utc_now
from scan_batch import ScanBatch


DATA_HUB_URL = "https://www.aofm.gov.au/data-hub"
FORTHCOMING_TRANSACTIONS_URL = "https://www.aofm.gov.au/program/forthcoming-transactions"
# The Data Hub publishes dated files.  This is a public fallback and callers
# normally discover the current link from DATA_HUB_URL on every uncached run.
TREASURY_BOND_ISSUANCE_URL = (
    "https://www.aofm.gov.au/sites/default/files/2025-06-20/treasury%20bonds%20-%20issuance.xlsx"
)
VENUE = "AUSTRALIAN_OFFICE_OF_FINANCIAL_MANAGEMENT"
MARKET_SURFACE = "australian_treasury_bond_tenders_and_results"


class AofmTreasuryBondTenderParseError(ValueError):
    """Raised when an AOFM public tender publication changes materially."""


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AofmTreasuryBondTenderParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _field(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _decimal(value: Any) -> float | None:
    text = str(value or "").strip().replace("\u00a0", " ")
    if not text or text.lower() in {"-", "n/a", "na", "nil"}:
        return None
    normalized = re.sub(r"[^0-9.+-]", "", text.replace(",", ""))
    try:
        return float(normalized)
    except ValueError:
        return None


def _amount_millions(value: Any, header: str | None) -> float | None:
    amount = _decimal(value)
    if amount is None:
        return None
    return amount if "million" in str(header or "").lower() else amount / 1_000_000.0


def _date(value: Any) -> dt.date | None:
    if isinstance(value, (int, float)):
        try:
            return dt.date(1899, 12, 30) + dt.timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None
    text = re.sub(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*", "", str(value or ""), flags=re.I)
    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text, flags=re.I).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _freshness(event_date: dt.date | None, fetched_at: dt.datetime, stale_after_hours: float) -> tuple[str, float | None]:
    if event_date is None:
        return "unknown", None
    age = max(0.0, (fetched_at.date() - event_date).total_seconds())
    return ("fresh" if age <= max(0.0, stale_after_hours) * 3600 else "stale", round(age, 3))


def _bond_parts(series: str) -> tuple[float | None, str | None]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s+(\d{1,2}\s+[A-Za-z]+\s+20\d{2})", series)
    return (float(match.group(1)), match.group(2)) if match else (None, None)


def parse_aofm_forthcoming_transactions(
    document: str,
    *,
    source_url: str = FORTHCOMING_TRANSACTIONS_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the AOFM's public scheduled Treasury Bond tender table."""

    if not isinstance(document, str) or not document.strip():
        raise AofmTreasuryBondTenderParseError("forthcoming-transactions response is empty")
    if not re.search(r"Treasury\s+Bonds", document, re.I):
        raise AofmTreasuryBondTenderParseError("Treasury Bonds marker was not found")
    fetched_at = _received_time(received_at)
    rows: list[dict[str, Any]] = []
    for table in html_tables(document):
        labels = {_field(row[0]): row[1:] for row in table if len(row) > 1}
        series = labels.get("seriesoffered")
        isins = labels.get("isin")
        tender_dates = labels.get("tenderdate")
        if not series or not isins or not tender_dates:
            continue
        amounts = labels.get("offeredtopublicmillion", labels.get("offeredtopublic")) or []
        bid_times = labels.get("timetosubmitbids") or []
        settlements = labels.get("settlementdate") or []
        for index, security in enumerate(series):
            security = " ".join(str(security or "").split())
            isin = re.sub(r"\s+", "", str(isins[index] if index < len(isins) else "")).upper()
            tender_date = _date(tender_dates[index] if index < len(tender_dates) else None)
            if not security or not isin or tender_date is None:
                continue
            coupon, maturity = _bond_parts(security)
            amount = _decimal(amounts[index] if index < len(amounts) else None)
            bid_window = " ".join(str(bid_times[index] if index < len(bid_times) else "").split()) or None
            settlement = _date(settlements[index] if index < len(settlements) else None)
            rows.append(
                {
                    "venue": VENUE,
                    "inst_id": f"{VENUE}:TBOND:SCHEDULED:{slug(isin)}:{tender_date.isoformat()}",
                    "instrument_id": f"{VENUE}:TBOND:SCHEDULED:{slug(isin)}:{tender_date.isoformat()}",
                    "symbol": isin,
                    "name": f"Australian Treasury Bond tender {security}",
                    "base": isin,
                    "quote": "AUD_PER_100_FACE",
                    "market_type": "sovereign_treasury_bond_tender_reference",
                    "market_surface": MARKET_SURFACE,
                    "asset_class": "australian_government_treasury_bond",
                    "trade_type": "official_scheduled_primary_tender",
                    "direction": "watch_only",
                    "last": 0.0,
                    "series_offered": security,
                    "isin": isin,
                    "coupon_pct": coupon,
                    "maturity_date": maturity,
                    "offered_to_public_millions_aud": amount,
                    "tender_date": tender_date.isoformat(),
                    "bid_submission_window_aest": bid_window,
                    "settlement_date": settlement.isoformat() if settlement else None,
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_scheduled_tender",
                    "freshness_state": "fresh",
                    "freshness_basis": "forthcoming_transactions_page_fetch_timestamp",
                    "freshness_age_seconds": 0.0,
                    "session_status": "tender_scheduled",
                    "observed_at": fetched_at.isoformat(),
                    "fetched_at": fetched_at.isoformat(),
                    "price_source": "AOFM forthcoming transactions",
                    "source_url": source_url,
                    "candidate_reject_reason": "official_tender_schedule_not_executable_quote",
                }
            )
    if not rows:
        raise AofmTreasuryBondTenderParseError("no Treasury Bond tender rows were found")
    return rows


def discover_aofm_treasury_bond_issuance_url(document: str, *, source_url: str = DATA_HUB_URL) -> str:
    """Find the current public Treasury Bond issuance workbook linked by AOFM."""

    if not isinstance(document, str) or not document.strip():
        raise AofmTreasuryBondTenderParseError("Data Hub response is empty")
    matches = re.findall(r"href\s*=\s*[\"']([^\"']+\.xlsx(?:\?[^\"']*)?)[\"']", document, re.I)
    for link in matches:
        candidate = html.unescape(link).strip()
        normalized = candidate.lower().replace("%20", " ")
        if "treasury" in normalized and "bond" in normalized and "issuance" in normalized and "indexed" not in normalized:
            return urllib.parse.urljoin(source_url, candidate)
    raise AofmTreasuryBondTenderParseError("Treasury Bond issuance workbook link was not found")


def _xlsx_sheets(content: bytes) -> list[list[dict[int, Any]]]:
    if not isinstance(content, bytes) or not content:
        raise AofmTreasuryBondTenderParseError("Treasury Bond issuance workbook is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as exc:
        raise AofmTreasuryBondTenderParseError(f"invalid Treasury Bond issuance XLSX: {exc}") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > 25_000_000:
            raise AofmTreasuryBondTenderParseError("expanded Treasury Bond issuance XLSX exceeds 25000000 byte limit")
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise AofmTreasuryBondTenderParseError(f"invalid XLSX shared strings: {exc}") from exc
            shared = ["".join(node.text or "" for node in item.iter(f"{namespace}t")) for item in root.findall(f"{namespace}si")]
        sheets: list[list[dict[int, Any]]] = []
        for name in sorted(item for item in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item)):
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError as exc:
                raise AofmTreasuryBondTenderParseError(f"invalid XLSX worksheet: {exc}") from exc
            sheet: list[dict[int, Any]] = []
            for row_node in root.iter(f"{namespace}row"):
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
                    if cell.get("t") == "s" and raw is not None:
                        try:
                            value: Any = shared[int(raw)]
                        except (IndexError, ValueError) as exc:
                            raise AofmTreasuryBondTenderParseError("XLSX shared string index is invalid") from exc
                    elif cell.get("t") == "inlineStr":
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
                    sheet.append(row)
            sheets.append(sheet)
    return sheets


def _header_field(headers: dict[int, str], *terms: str) -> int | None:
    for column, header in headers.items():
        token = _field(header)
        if all(term in token for term in terms):
            return column
    return None


def parse_aofm_treasury_bond_issuance_workbook(
    content: bytes,
    *,
    source_url: str = TREASURY_BOND_ISSUANCE_URL,
    received_at: str | None = None,
    stale_after_hours: float = 168.0,
) -> list[dict[str, Any]]:
    """Normalize AOFM's public Treasury Bond tender-result workbook."""

    fetched_at = _received_time(received_at)
    observations: list[dict[str, Any]] = []
    for sheet in _xlsx_sheets(content):
        for position, raw_header in enumerate(sheet):
            headers = {column: str(value or "") for column, value in raw_header.items()}
            series_column = _header_field(headers, "series") or _header_field(headers, "security")
            tender_date_column = (
                _header_field(headers, "tender", "date")
                or _header_field(headers, "auction", "date")
                or _header_field(headers, "date", "held")
            )
            maturity_column = _header_field(headers, "maturity")
            coupon_column = _header_field(headers, "coupon")
            if tender_date_column is None or (
                series_column is None and (maturity_column is None or coupon_column is None)
            ):
                continue
            isin_column = _header_field(headers, "isin")
            tender_number_column = _header_field(headers, "tender", "number")
            offered_column = _header_field(headers, "offered")
            allotted_column = _header_field(headers, "allotted") or _header_field(headers, "issued")
            yield_column = _header_field(headers, "weighted", "yield") or _header_field(headers, "yield")
            price_column = _header_field(headers, "weighted", "price") or _header_field(headers, "average", "price")
            cover_column = _header_field(headers, "cover")
            settlement_column = _header_field(headers, "settlement", "date")
            if settlement_column is None:
                settlement_column = _header_field(headers, "date", "settled")
            for row_number, raw in enumerate(sheet[position + 1 :], start=position + 2):
                series = " ".join(str(raw.get(series_column) or "").split()) if series_column else ""
                tender_date = _date(raw.get(tender_date_column))
                maturity = _date(raw.get(maturity_column)) if maturity_column else None
                coupon_value = _decimal(raw.get(coupon_column)) if coupon_column else None
                if not series and maturity is not None and coupon_value is not None:
                    series = f"{coupon_value:.2f}% {maturity.strftime('%d %B %Y')}"
                if not series or tender_date is None:
                    continue
                isin = re.sub(r"\s+", "", str(raw.get(isin_column) or "")).upper() if isin_column else ""
                coupon, maturity_text = _bond_parts(series)
                instrument = isin or slug(series)
                freshness_state, freshness_age = _freshness(tender_date, fetched_at, stale_after_hours)
                average_price = _decimal(raw.get(price_column)) if price_column else None
                settlement_date = _date(raw.get(settlement_column)) if settlement_column else None
                observations.append(
                    {
                        "venue": VENUE,
                        "inst_id": f"{VENUE}:TBOND:RESULT:{slug(instrument)}:{tender_date.isoformat()}:{row_number}",
                        "instrument_id": f"{VENUE}:TBOND:RESULT:{slug(instrument)}:{tender_date.isoformat()}:{row_number}",
                        "symbol": isin or slug(series),
                        "name": f"Australian Treasury Bond tender result {series}",
                        "base": isin or slug(series),
                        "quote": "AUD_PER_100_FACE",
                        "market_type": "sovereign_treasury_bond_tender_result_reference",
                        "market_surface": MARKET_SURFACE,
                        "asset_class": "australian_government_treasury_bond",
                        "trade_type": "official_primary_tender_result",
                        "direction": "watch_only",
                        "last": average_price if average_price is not None else 0.0,
                        "series_offered": series,
                        "isin": isin or None,
                        "tender_number": str(raw.get(tender_number_column) or "") or None if tender_number_column else None,
                        "coupon_pct": coupon if coupon is not None else coupon_value,
                        "maturity_date": maturity_text or (maturity.isoformat() if maturity else None),
                        "maturity_date_iso": maturity.isoformat() if maturity else _date(maturity_text).isoformat() if _date(maturity_text) else None,
                        "tender_date": tender_date.isoformat(),
                        "settlement_date": settlement_date.isoformat() if settlement_date else None,
                        "offered_to_public_millions_aud": _amount_millions(raw.get(offered_column), headers.get(offered_column)) if offered_column else None,
                        "allotted_millions_aud": _amount_millions(raw.get(allotted_column), headers.get(allotted_column)) if allotted_column else None,
                        "weighted_average_yield_pct": _decimal(raw.get(yield_column)) if yield_column else None,
                        "weighted_average_price_per_100": average_price,
                        "bid_cover_ratio": _decimal(raw.get(cover_column)) if cover_column else None,
                        "data_status": "reachable",
                        "fetch_status": "reachable",
                        "quality_status": "official_tender_result",
                        "freshness_state": freshness_state,
                        "freshness_basis": "official_tender_date",
                        "freshness_age_seconds": freshness_age,
                        "session_status": "results_published",
                        "observed_at": fetched_at.isoformat(),
                        "fetched_at": fetched_at.isoformat(),
                        "price_source": "AOFM Treasury Bonds issuance Data Hub workbook",
                        "source_url": source_url,
                        "source_page_url": DATA_HUB_URL,
                        "candidate_reject_reason": "official_tender_result_not_executable_quote",
                    }
                )
    if not observations:
        raise AofmTreasuryBondTenderParseError("Treasury Bond issuance workbook has no usable tender-result rows")
    return observations


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


def _failure_observation(result: dict[str, Any], source_url: str, parser_error: str | None = None) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "market_type": "sovereign_treasury_bond_tender_reference",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": "public_aofm_tender_parser_failure" if parser_error else "public_aofm_tender_source_unavailable",
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class AustralianOfficeOfFinancialManagementAofmAdapter:
    info = AdapterInfo(
        adapter_id="australian_office_of_financial_management_aofm",
        venue=VENUE,
        market_type="sovereign_treasury_bond_tender_reference",
        source="Australian Office of Financial Management Treasury Bond tenders and results",
        capabilities=("public_market_data", "auction_results", "auction_schedule", "auction_yield", "award_size", "event_price_reference", "source_health"),
        aliases=("australian office of financial management", "aofm", "australian treasury bonds", "aofm treasury bond tenders"),
        docs_url=DATA_HUB_URL,
        runtime_entrypoint="adapters.venues.australian_office_of_financial_management_aofm.AustralianOfficeOfFinancialManagementAofmAdapter",
        quote_assets=("AUD_PER_100_FACE",),
        default_cache_minutes=30,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        forthcoming_url = str(cfg.get("forthcoming_transactions_url") or FORTHCOMING_TRANSACTIONS_URL)
        hub_url = str(cfg.get("data_hub_url") or DATA_HUB_URL)
        fetch_status: dict[str, dict[str, Any]] = {}
        parser_failures: list[dict[str, str]] = []
        observations: list[dict[str, Any]] = []

        forthcoming = fetch_text(forthcoming_url, timeout)
        fetch_status["forthcoming_transactions"] = _fetch_evidence(forthcoming, forthcoming_url)
        if forthcoming.get("ok"):
            try:
                observations.extend(parse_aofm_forthcoming_transactions(str(forthcoming.get("text") or ""), source_url=forthcoming_url, received_at=forthcoming.get("received_at")))
            except (AofmTreasuryBondTenderParseError, TypeError, ValueError) as exc:
                message = f"AOFM forthcoming-transactions parser failed: {exc}"[:300]
                parser_failures.append({"source_url": forthcoming_url, "error": message})
                observations.append(_failure_observation(forthcoming, forthcoming_url, message))
        else:
            observations.append(_failure_observation(forthcoming, forthcoming_url))

        hub = fetch_text(hub_url, timeout)
        fetch_status["data_hub"] = _fetch_evidence(hub, hub_url)
        workbook_url: str | None = str(cfg.get("tender_results_workbook_url") or "") or None
        if hub.get("ok"):
            if workbook_url is None:
                try:
                    workbook_url = discover_aofm_treasury_bond_issuance_url(str(hub.get("text") or ""), source_url=hub_url)
                except (AofmTreasuryBondTenderParseError, TypeError, ValueError) as exc:
                    message = f"AOFM Data Hub parser failed: {exc}"[:300]
                    parser_failures.append({"source_url": hub_url, "error": message})
                    observations.append(_failure_observation(hub, hub_url, message))
        else:
            observations.append(_failure_observation(hub, hub_url))
        if workbook_url:
            workbook = fetch_bytes(workbook_url, timeout)
            fetch_status["treasury_bond_issuance_workbook"] = _fetch_evidence(workbook, workbook_url)
            if workbook.get("ok"):
                try:
                    observations.extend(
                        parse_aofm_treasury_bond_issuance_workbook(
                            workbook.get("content") or b"",
                            source_url=workbook_url,
                            received_at=workbook.get("received_at"),
                            stale_after_hours=max(
                                0.0, float(cfg.get("stale_after_hours", 168.0))
                            ),
                        )
                    )
                except (AofmTreasuryBondTenderParseError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                    message = f"AOFM tender-results workbook parser failed: {exc}"[:300]
                    parser_failures.append({"source_url": workbook_url, "error": message})
                    observations.append(_failure_observation(workbook, workbook_url, message))
            else:
                observations.append(_failure_observation(workbook, workbook_url))

        real = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real})
        source_status = "reachable" if real and not parser_failures else "degraded" if parser_failures else "unavailable"
        if not real and not parser_failures:
            source_status = next((str(item["fetch_status"]) for item in fetch_status.values() if item["fetch_status"] != "reachable"), "unavailable")
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1062,
                "source_status": source_status,
                "source_url": forthcoming_url,
                "source_urls": [forthcoming_url, hub_url, *( [workbook_url] if workbook_url else [] )],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real),
                "capability_gap": "public_entry_quality_secondary_market_quote_and_order_book_not_available",
                "paper_only": True,
            },
        )


AofmAdapter = AustralianOfficeOfFinancialManagementAofmAdapter
register_adapter(AustralianOfficeOfFinancialManagementAofmAdapter())
