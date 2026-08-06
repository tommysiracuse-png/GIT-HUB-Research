"""NSX public weekly-report adapter for selected dual-listed Satrix feeder ETFs."""

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
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, number
from scan_batch import ScanBatch


MARKET_INFO_URL = "https://nsx.com.na/trading/membership/market-info/"
MARKET_SURFACE = "nsx_satrix_dual_listed_feeder_etfs"
VENUE = "NSX"
REPORT_SHEET_NAME = "ETP & DevX"
NSX_TIME = dt.timezone(dt.timedelta(hours=2))
TARGET_INSTRUMENTS = (
    {
        "symbol": "SXN500",
        "name": "Satrix S&P 500 Feeder NM",
        "underlying_symbol": "STX500",
        "isin": "ZAE000246641",
        "exposure": "S&P 500",
    },
    {
        "symbol": "SXNWDM",
        "name": "Satrix MSCI World Feeder NM",
        "underlying_symbol": "STXWDM",
        "isin": "ZAE000246104",
        "exposure": "MSCI World",
    },
)


class NamibiaSecuritiesExchangeParseError(ValueError):
    """Raised when the reachable NSX weekly-report surface drifts."""


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _received_time(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NamibiaSecuritiesExchangeParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _excel_column(label: str) -> int:
    total = 0
    for char in str(label):
        total = total * 26 + ord(char) - 64
    return total


def _sheet_name_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def discover_latest_report_url(document: str, *, source_url: str = MARKET_INFO_URL) -> str:
    """Find the latest NSX weekly XLSX report linked from the public market-info page."""

    if not isinstance(document, str) or not document.strip():
        raise NamibiaSecuritiesExchangeParseError("NSX market-info page is empty")
    links = re.findall(
        r"(?:href|data-download-url)\s*=\s*[\"']([^\"']+NSX-Weekly[^\"']+\.xlsx(?:\?[^\"']*)?)[\"']",
        document,
        flags=re.IGNORECASE,
    )
    candidates = [html.unescape(link).strip() for link in links]
    if not candidates:
        raise NamibiaSecuritiesExchangeParseError("NSX weekly XLSX report link was not found")
    return urllib.parse.urljoin(source_url, candidates[0])


def _xlsx_sheet_rows(content: bytes) -> dict[str, list[dict[int, Any]]]:
    if not isinstance(content, bytes) or not content:
        raise NamibiaSecuritiesExchangeParseError("NSX weekly XLSX response is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as exc:
        raise NamibiaSecuritiesExchangeParseError(f"invalid NSX weekly XLSX: {exc}") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > 25_000_000:
            raise NamibiaSecuritiesExchangeParseError("expanded NSX weekly XLSX exceeds 25000000 byte limit")
        if "xl/workbook.xml" not in archive.namelist() or "xl/_rels/workbook.xml.rels" not in archive.namelist():
            raise NamibiaSecuritiesExchangeParseError("NSX weekly XLSX workbook metadata is missing")
        ns = {
            "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
        }
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise NamibiaSecuritiesExchangeParseError(f"invalid NSX shared strings: {exc}") from exc
            shared = [
                "".join(node.text or "" for node in item.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
                for item in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si")
            ]
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except ET.ParseError as exc:
            raise NamibiaSecuritiesExchangeParseError(f"invalid NSX workbook XML: {exc}") from exc
        relmap = {
            rel.get("Id"): rel.get("Target")
            for rel in rels.findall("rel:Relationship", ns)
            if rel.get("Id") and rel.get("Target")
        }
        sheets: dict[str, list[dict[int, Any]]] = {}
        for sheet in workbook.findall("s:sheets/s:sheet", ns):
            name = str(sheet.get("name") or "").strip()
            relationship_id = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = relmap.get(relationship_id or "")
            if not name or not target:
                continue
            path = target if target.startswith("xl/") else "xl/" + target.removeprefix("/")
            if path not in archive.namelist():
                continue
            try:
                worksheet = ET.fromstring(archive.read(path))
            except ET.ParseError as exc:
                raise NamibiaSecuritiesExchangeParseError(f"invalid NSX worksheet XML: {exc}") from exc
            rows: list[dict[int, Any]] = []
            for row_node in worksheet.findall(".//s:sheetData/s:row", ns):
                row: dict[int, Any] = {}
                for cell in row_node.findall("s:c", ns):
                    ref_match = re.match(r"([A-Z]+)", str(cell.get("r") or ""))
                    if not ref_match:
                        continue
                    column = _excel_column(ref_match.group(1))
                    raw_node = cell.find("s:v", ns)
                    raw = raw_node.text if raw_node is not None else None
                    cell_type = cell.get("t")
                    if cell_type == "s" and raw is not None:
                        try:
                            value: Any = shared[int(raw)]
                        except (IndexError, ValueError) as exc:
                            raise NamibiaSecuritiesExchangeParseError("NSX shared string index is invalid") from exc
                    elif cell_type == "inlineStr":
                        value = "".join(
                            node.text or ""
                            for node in cell.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                        )
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
            sheets[name] = rows
        if not sheets:
            raise NamibiaSecuritiesExchangeParseError("NSX weekly XLSX contains no readable worksheets")
        return sheets


def _report_window(value: Any) -> tuple[dt.date, dt.date] | None:
    text = " ".join(str(value or "").replace("\n", " ").split())
    match = re.search(
        r"(\d{1,2})\s+([A-Za-z]+)\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})",
        text,
    )
    if not match:
        return None
    try:
        start = dt.datetime.strptime(
            f"{match.group(1)} {match.group(2)} {match.group(5)}", "%d %B %Y"
        ).date()
        end = dt.datetime.strptime(
            f"{match.group(3)} {match.group(4)} {match.group(5)}", "%d %B %Y"
        ).date()
    except ValueError:
        return None
    return start, end


def _session_status(_fetched_at: dt.datetime) -> str:
    return "weekly_report_reference"


def _header_fields(row: dict[int, Any]) -> dict[str, int]:
    tokens = {_token(value): column for column, value in row.items() if _token(value)}
    required = {
        "symbol": ("sharecode",),
        "isin": ("isin",),
        "last": ("nsxendofdayprice",),
        "previous_close": ("nsxpreviousclose",),
        "traded_volume": ("tradedvolume",),
        "traded_value": ("daystotalvalue",),
        "deals": ("deals",),
        "day_high": ("dayhigh",),
        "day_low": ("daylow",),
        "shares_in_issue": ("totalofsharesinissue",),
        "market_cap": ("marketcapatclose", "marketcapatcloseoftrade"),
    }
    fields: dict[str, int] = {}
    for name, options in required.items():
        for option in options:
            if option in tokens:
                fields[name] = tokens[option]
                break
    minimum = {"symbol", "last", "previous_close", "traded_volume", "shares_in_issue"}
    if not minimum.issubset(fields):
        missing = sorted(minimum - set(fields))
        raise NamibiaSecuritiesExchangeParseError(
            "NSX weekly ETF header is missing fields: " + ", ".join(missing)
        )
    return fields


def parse_nsx_weekly_report(
    content: bytes,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 28.0,
) -> list[dict[str, Any]]:
    """Normalize the current weekly NSX report for SXN500 and SXNWDM."""

    sheets = _xlsx_sheet_rows(content)
    report_sheet = next(
        (rows for name, rows in sheets.items() if _sheet_name_token(name) == _sheet_name_token(REPORT_SHEET_NAME)),
        None,
    )
    if report_sheet is None:
        raise NamibiaSecuritiesExchangeParseError("NSX weekly ETF worksheet was not found")
    fetched_at = _received_time(received_at)
    report_period = None
    for row in report_sheet[:10]:
        for value in row.values():
            report_period = _report_window(value)
            if report_period:
                break
        if report_period:
            break
    if report_period is None:
        raise NamibiaSecuritiesExchangeParseError("NSX weekly report period was not found")
    period_start, period_end = report_period
    header_index = None
    fields: dict[str, int] = {}
    for index, row in enumerate(report_sheet):
        try:
            candidate_fields = _header_fields(row)
        except NamibiaSecuritiesExchangeParseError:
            continue
        header_index = index
        fields = candidate_fields
        break
    if header_index is None:
        raise NamibiaSecuritiesExchangeParseError("NSX weekly ETF header row was not found")

    by_symbol = {item["symbol"]: item for item in TARGET_INSTRUMENTS}
    observations: dict[str, dict[str, Any]] = {}
    age_seconds = max(
        0.0,
        (
            fetched_at.astimezone(dt.timezone.utc)
            - dt.datetime.combine(period_end, dt.time(17, 0), tzinfo=NSX_TIME).astimezone(dt.timezone.utc)
        ).total_seconds(),
    )
    freshness_state = (
        "fresh" if age_seconds <= max(0.0, float(stale_after_days)) * 86400.0 else "stale"
    )
    session_status = _session_status(fetched_at)
    for row in report_sheet[header_index + 1 :]:
        symbol = str(row.get(fields["symbol"]) or "").strip().upper()
        if symbol not in by_symbol:
            continue
        last = number(row.get(fields["last"]))
        previous_close = number(row.get(fields["previous_close"]))
        if last is None or last <= 0 or previous_close is None or previous_close <= 0:
            continue
        instrument = by_symbol[symbol]
        inst_id = f"{VENUE}:ETF:{symbol}"
        observations[symbol] = {
            "venue": VENUE,
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": symbol,
            "name": str(row.get(_excel_column("C")) or instrument["name"]),
            "base": str(instrument["underlying_symbol"]),
            "quote": "NAD",
            "market_type": "exchange_traded_fund",
            "market_surface": MARKET_SURFACE,
            "asset_class": "dual_listed_feeder_etf",
            "trade_type": "official_weekly_trading_report",
            "direction": "watch_only",
            "last": round(last, 6),
            "previous_close": round(previous_close, 6),
            "traded_volume": int(number(row.get(fields["traded_volume"])) or 0),
            "traded_value": number(row.get(fields.get("traded_value"))),
            "deal_count": int(number(row.get(fields.get("deals"))) or 0),
            "day_high": number(row.get(fields.get("day_high"))),
            "day_low": number(row.get(fields.get("day_low"))),
            "shares_in_issue": int(number(row.get(fields["shares_in_issue"])) or 0),
            "market_cap": number(row.get(fields.get("market_cap"))),
            "isin": str(row.get(fields.get("isin")) or instrument["isin"]),
            "underlying_exchange": "JSE",
            "underlying_symbol": str(instrument["underlying_symbol"]),
            "benchmark_exposure": str(instrument["exposure"]),
            "public_route_status": "route_needed",
            "paper_experiment_eligible": True,
            "data_delay": "weekly_report",
            "report_period_start": period_start.isoformat(),
            "report_period_end": period_end.isoformat(),
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_weekly_exchange_report",
            "freshness_state": freshness_state,
            "freshness_basis": "nsx_weekly_report_period_end",
            "freshness_age_seconds": round(age_seconds, 3),
            "session_status": session_status,
            "session_basis": "nsx_public_weekly_report_reference",
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Namibia Securities Exchange weekly trading report",
            "source_url": source_url,
            "source_page_url": MARKET_INFO_URL,
        }
    missing = [item["symbol"] for item in TARGET_INSTRUMENTS if item["symbol"] not in observations]
    if missing:
        raise NamibiaSecuritiesExchangeParseError(
            "NSX weekly report is missing target ETF rows: " + ", ".join(missing)
        )
    return [observations[item["symbol"]] for item in TARGET_INSTRUMENTS]


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
    instrument: dict[str, str],
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    symbol = str(instrument["symbol"])
    row.update(
        {
            "inst_id": f"{VENUE}:ETF:{symbol}:HEALTH",
            "instrument_id": f"{VENUE}:ETF:{symbol}:HEALTH",
            "symbol": symbol,
            "name": instrument["name"],
            "base": instrument["underlying_symbol"],
            "quote": "NAD",
            "market_type": "exchange_traded_fund",
            "asset_class": "dual_listed_feeder_etf",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "session_basis": "source_unavailable",
            "underlying_exchange": "JSE",
            "underlying_symbol": instrument["underlying_symbol"],
            "isin": instrument["isin"],
            "public_route_status": "route_needed",
            "paper_experiment_eligible": False,
            "source_page_url": MARKET_INFO_URL,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_nsx_weekly_report_parser_failure"
                if parser_error
                else "public_nsx_weekly_report_source_unavailable"
            ),
        }
    )
    return row


class NamibiaSecuritiesExchangeAdapter:
    info = AdapterInfo(
        adapter_id="namibia_securities_exchange_nsx_satrix_feeders",
        venue=VENUE,
        market_type="exchange_traded_fund",
        source="NSX public weekly trading reports",
        capabilities=(
            "public_market_data",
            "delayed_quote",
            "exchange_traded_fund",
            "weekly_report",
            "source_health",
        ),
        aliases=(
            "namibia securities exchange",
            "nsx",
            "satrix s&p 500 feeder",
            "satrix msci world feeder",
            "sxn500",
            "sxnwdm",
        ),
        docs_url=MARKET_INFO_URL,
        runtime_entrypoint="adapters.venues.namibia_securities_exchange.NamibiaSecuritiesExchangeAdapter",
        quote_assets=("NAD",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 20)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 28.0)))
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}

        page_result = fetch_text(MARKET_INFO_URL, timeout)
        fetch_status["market_info_page"] = _fetch_evidence(page_result, MARKET_INFO_URL)
        source_urls = [MARKET_INFO_URL]

        if not page_result.get("ok"):
            observations = [_failure_observation(item, MARKET_INFO_URL, page_result) for item in TARGET_INSTRUMENTS]
            source_status = str(page_result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                workbook_url = str(
                    cfg.get("workbook_url")
                    or discover_latest_report_url(str(page_result.get("text") or ""))
                )
            except (NamibiaSecuritiesExchangeParseError, TypeError, ValueError) as exc:
                message = f"NSX market-info parser failed: {exc}"[:300]
                parser_failures.append({"source_url": MARKET_INFO_URL, "error": message})
                observations = [
                    _failure_observation(item, MARKET_INFO_URL, page_result, message)
                    for item in TARGET_INSTRUMENTS
                ]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"
            else:
                source_urls.append(workbook_url)
                workbook_result = fetch_bytes(workbook_url, timeout)
                fetch_status["weekly_report"] = _fetch_evidence(workbook_result, workbook_url)
                if not workbook_result.get("ok"):
                    observations = [
                        _failure_observation(item, workbook_url, workbook_result)
                        for item in TARGET_INSTRUMENTS
                    ]
                    source_status = str(workbook_result.get("status") or "unavailable")
                    freshness_state = "unknown"
                    session_state = "unknown"
                else:
                    try:
                        observations = parse_nsx_weekly_report(
                            workbook_result.get("content") or b"",
                            source_url=workbook_url,
                            received_at=workbook_result.get("received_at"),
                            stale_after_days=stale_after_days,
                        )
                        source_status = "reachable"
                        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
                        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
                        freshness_state = (
                            freshness_states[0]
                            if len(freshness_states) == 1
                            else "mixed"
                            if freshness_states
                            else "unknown"
                        )
                        session_state = (
                            session_states[0]
                            if len(session_states) == 1
                            else "mixed"
                            if session_states
                            else "unknown"
                        )
                    except (NamibiaSecuritiesExchangeParseError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                        message = f"NSX weekly-report parser failed: {exc}"[:300]
                        parser_failures.append({"source_url": workbook_url, "error": message})
                        observations = [
                            _failure_observation(item, workbook_url, workbook_result, message)
                            for item in TARGET_INSTRUMENTS
                        ]
                        source_status = "degraded"
                        freshness_state = "unknown"
                        session_state = "unknown"

        real_rows = [row for row in observations if row.get("last") not in (None, 0, 0.0) and not row.get("parser_failure")]
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 364,
                "source_status": source_status,
                "source_url": MARKET_INFO_URL,
                "source_urls": source_urls,
                "fetch_status": fetch_status,
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_entry_quality_quotes_and_order_route",
                "paper_only": True,
            },
        )


register_adapter(NamibiaSecuritiesExchangeAdapter())
