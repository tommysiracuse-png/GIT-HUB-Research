"""EEX public EU ETS and German nEHS result adapters.

Official auction files and reported trades are references, not executable
quotes. They remain watch-only even when fresh so paper mode cannot turn a
published result into an order route.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, number, parse_json, utc_now
from scan_batch import ScanBatch


DATA_PAGE_URL = "https://www.eex.com/en/markets/environmentals/german-nehs/german-nehs-data"
SALES_URL = "https://public.eex-group.com/eex/nehs-reporting/nEHS_sale_Reporting.csv"
AUCTION_URL = "https://public.eex-group.com/eex/nehs-reporting/nEHS_Auction_Reporting.csv"
DATASOURCE_DOCS_URL = (
    "https://www.eex.com/fileadmin/EEX/Downloads/Market_Data/EEX_Group_DataSource/API/"
    "20251006_EEX_Market_Data_API_-_First_Steps_v1.00.pdf"
)
EUA_AUCTION_PAGE_URL = (
    "https://www.eex.com/en/market-data/market-data-hub/environmentals/"
    "eex-eua-primary-auction-spot-download"
)
DATASOURCE_API_ROOT = "https://api1.datasource.eex-group.com"
UK_ETS_PAGE_URL = "https://www.eex.com/en/markets/environmentals/uk-ets"
UKA_FIRST_TRADEABLE_EXPIRY = "2026-12"


def eua_auction_report_url(year: int) -> str:
    return (
        "https://public.eex-group.com/eex/eua-auction-report/"
        f"emission-spot-primary-market-auction-report-{int(year)}-data.xlsx"
    )


EUA_AUCTION_REPORT_URL = eua_auction_report_url(dt.datetime.now(dt.timezone.utc).year)


class EexNehsParseError(ValueError):
    """Raised when a reachable EEX report no longer matches its public schema."""


class EexEuEtsParseError(ValueError):
    """Raised when reachable EU ETS auction or spot data no longer matches its public schema."""


class EexUkaParseError(ValueError):
    """Raised when the public EEX UKA contract-specification page changes schema."""


class _EexVisibleTextParser(HTMLParser):
    """Extract visible EEX page text without treating scripts as contract data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _eex_visible_text(document: str) -> str:
    if not isinstance(document, str) or not document.strip():
        raise EexUkaParseError("UK ETS contract page response is empty")
    parser = _EexVisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - surface parser evidence rather than losing it.
        raise EexUkaParseError(f"invalid UK ETS contract page HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _uka_contract_observation(
    *,
    instrument: str,
    name: str,
    market_type: str,
    source_url: str,
    fetched_at: str,
    contract_volume_uka: int,
    minimum_tick_gbp_per_uka: float,
) -> dict[str, Any]:
    inst_id = f"EEX:UKA:{instrument}:DEC2026"
    return {
        "venue": "EEX",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": f"UKA_{instrument}_DEC2026",
        "name": name,
        "base": "UKA",
        "quote": "GBP_PER_UKA",
        "market_type": market_type,
        "market_surface": "eex_uk_ets_uka_futures_options",
        "asset_class": "emission_allowance_derivative",
        "trade_type": "official_contract_specification",
        "direction": "watch_only",
        "last": 0.0,
        "price_basis": "contract_catalog_only",
        "underlying": "UK ETS Allowance (UKA)",
        "underlying_unit": "tonne_co2e",
        "first_tradeable_expiry": UKA_FIRST_TRADEABLE_EXPIRY,
        "delivery_month": "December 2026",
        "contract_volume_uka": contract_volume_uka,
        "minimum_tick_gbp_per_uka": minimum_tick_gbp_per_uka,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_contract_specification",
        "freshness_state": "fresh",
        "freshness_basis": "official_contract_specification_page_fetch",
        "freshness_age_seconds": 0.0,
        "session_status": "reference_only",
        "observed_at": fetched_at,
        "fetched_at": fetched_at,
        "price_source": "EEX UK ETS Futures and Options contract specifications",
        "source_url": source_url,
        "candidate_reject_reason": "public_contract_specification_not_executable_quote",
    }


def parse_eex_uka_futures_options(
    document: str,
    *,
    source_url: str = UK_ETS_PAGE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the EEX public UKA Futures and Options contract specifications.

    EEX publishes the contract terms, but not a stable anonymous quote or
    order-book endpoint on this page.  These are therefore real contract
    observations only; every row remains explicitly watch-only.
    """

    text = _eex_visible_text(document)
    required_terms = ("UK ETS Futures and Options", "UKA Futures", "UKA Options")
    missing = [term for term in required_terms if term.casefold() not in text.casefold()]
    if missing:
        raise EexUkaParseError(f"required UKA contract labels were not found: {', '.join(missing)}")
    first_expiry = re.search(
        r"first\s+(?:delivery\s+starts\s+)?(?:from\s+)?December\s+2026",
        text,
        flags=re.IGNORECASE,
    )
    if not first_expiry:
        raise EexUkaParseError("first UKA tradeable expiry December 2026 was not found")
    volume_match = re.search(r"Contract\s+volume\s*\|?\s*1\s*,?\s*000\s+UKA", text, flags=re.IGNORECASE)
    if not volume_match:
        raise EexUkaParseError("UKA Futures contract volume of 1,000 UKA was not found")
    tick_match = re.search(r"Minimum\s+tick\s*\|?\s*[£\u00a3]\s*0[.,]01\s+per\s+UKA", text, flags=re.IGNORECASE)
    if not tick_match:
        raise EexUkaParseError("UKA Futures minimum tick of GBP 0.01 per UKA was not found")
    if not re.search(r"Underlying\s*\|?\s*The\s+underlying\s+is\s+the\s+EEX\s+UKA\s+Dec\s+Futures", text, flags=re.IGNORECASE):
        raise EexUkaParseError("UKA Options underlying EEX UKA Dec Futures was not found")

    fetched_at = _received_time(received_at).isoformat()
    futures = _uka_contract_observation(
        instrument="FUTURE",
        name="EEX UK Emission Allowance (UKA) Futures December 2026",
        market_type="futures_catalog",
        source_url=source_url,
        fetched_at=fetched_at,
        contract_volume_uka=1_000,
        minimum_tick_gbp_per_uka=0.01,
    )
    options = _uka_contract_observation(
        instrument="OPTION",
        name="EEX UK Emission Allowance (UKA) Options on December 2026 Futures",
        market_type="options_catalog",
        source_url=source_url,
        fetched_at=fetched_at,
        contract_volume_uka=1_000,
        minimum_tick_gbp_per_uka=0.01,
    )
    options.update(
        {
            "option_style": "European",
            "option_underlying": "EEX UKA Dec Futures",
            "option_expiry_months": ("March", "December"),
        }
    )
    return [futures, options]


def _header_token(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lstrip("\ufeff"))
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).lower().split())


def _report_rows(text: str) -> tuple[list[str], list[list[str]]]:
    if not isinstance(text, str) or not text.strip():
        raise EexNehsParseError("empty CSV response")
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any("date" in _header_token(cell) for cell in row)
            and any("vintage" in _header_token(cell) for cell in row)
        ),
        None,
    )
    if header_index is None:
        raise EexNehsParseError("CSV header with Date and Vintage columns was not found")
    headers = [_header_token(cell) for cell in rows[header_index]]
    return headers, [row for row in rows[header_index + 1 :] if any(str(cell).strip() for cell in row)]


def _column(headers: list[str], *terms: str) -> int:
    for index, header in enumerate(headers):
        if all(term in header for term in terms):
            return index
    raise EexNehsParseError(f"required CSV column was not found: {' + '.join(terms)}")


def _optional_column(headers: list[str], *terms: str) -> int | None:
    try:
        return _column(headers, *terms)
    except EexNehsParseError:
        return None


def _cell(row: list[str], index: int | None) -> str:
    return str(row[index]).strip() if index is not None and index < len(row) else ""


def _event_time(date_value: str, time_value: str) -> dt.datetime | None:
    value = f"{str(date_value).strip()} {str(time_value).strip()}".strip()
    for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _freshness(event_time: dt.datetime, received_at: str | None, stale_after_hours: float) -> tuple[str, float]:
    age = max(0.0, (_received_time(received_at) - event_time).total_seconds())
    state = "fresh" if age <= max(0.0, float(stale_after_hours)) * 3600.0 else "stale"
    return state, round(age, 3)


def parse_eex_nehs_auction(
    text: str,
    *,
    source_url: str = AUCTION_URL,
    received_at: str | None = None,
    stale_after_hours: float = 192.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize the official bilingual nEHS auction-result CSV."""

    headers, rows = _report_rows(text)
    date_col = _column(headers, "date")
    time_col = _column(headers, "time")
    vintage_col = _column(headers, "vintage")
    price_col = _column(headers, "auction clearing price")
    allocated_col = _column(headers, "volume allocated")
    remaining_col = _optional_column(headers, "remaining auction volume")
    bids_col = _optional_column(headers, "total volume of bids")
    bidders_col = _optional_column(headers, "total number of bidders")
    successful_col = _optional_column(headers, "successful bidders")
    revenue_col = _optional_column(headers, "revenues")
    cover_col = _optional_column(headers, "cover ratio")
    actual_cover_col = next(
        (index for index, header in enumerate(headers) if "cover ratio" in header and "actual allocated" in header),
        None,
    )
    cancellation_col = _optional_column(headers, "potential cancellation")

    parsed: list[dict] = []
    invalid_rows = 0
    for row in rows:
        event_time = _event_time(_cell(row, date_col), _cell(row, time_col))
        vintage = _cell(row, vintage_col)
        price = number(_cell(row, price_col))
        allocated = number(_cell(row, allocated_col))
        if event_time is None or not vintage or price is None or price <= 0 or allocated is None:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        symbol = f"NEZ_{vintage}"
        parsed.append(
            {
                "venue": "EEX",
                "inst_id": f"EEX:{symbol}:AUCTION:{event_time.date().isoformat()}",
                "instrument_id": f"EEX:{symbol}:AUCTION:{event_time.date().isoformat()}",
                "symbol": symbol,
                "name": f"German nEHS national emissions certificate {vintage} auction",
                "base": symbol,
                "quote": "EUR_PER_NEZ",
                "market_type": "auction_reference",
                "market_surface": "eex_german_nehs_auction_results",
                "asset_class": "national_emissions_certificate",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "auction_clearing_price": price,
                "allocated_volume": allocated,
                "remaining_auction_volume": number(_cell(row, remaining_col)),
                "total_bid_volume": number(_cell(row, bids_col)),
                "bidder_count": int(number(_cell(row, bidders_col)) or 0),
                "successful_bidder_count": int(number(_cell(row, successful_col)) or 0),
                "revenue_eur": number(_cell(row, revenue_col)),
                "cover_ratio": number(_cell(row, cover_col)),
                "allocated_volume_cover_ratio": number(_cell(row, actual_cover_col)),
                "cancellation_notice": _cell(row, cancellation_col) or None,
                "vintage": int(vintage) if vintage.isdigit() else vintage,
                "event_date": event_time.date().isoformat(),
                "event_time_utc": event_time.isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_event_result",
                "freshness_state": freshness_state,
                "freshness_basis": "official_auction_close_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "EEX German nEHS auction report",
                "source_url": source_url,
                "candidate_reject_reason": "auction_result_reference_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} data rows were invalid" if invalid_rows else ""
        raise EexNehsParseError(f"no usable nEHS auction result rows{detail}")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def parse_eex_nehs_sales(
    text: str,
    *,
    source_url: str = SALES_URL,
    received_at: str | None = None,
    stale_after_hours: float = 192.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize the official bilingual nEHS fixed-price sales CSV."""

    headers, rows = _report_rows(text)
    date_col = _column(headers, "date")
    time_col = _column(headers, "time")
    vintage_col = _column(headers, "vintage")
    price_col = _column(headers, "price")
    volume_col = _column(headers, "volume")
    trades_col = _optional_column(headers, "number of trades")
    buyers_col = _optional_column(headers, "number of buyers")
    revenue_col = _optional_column(headers, "certificate-revenue")
    disclaimer_col = _optional_column(headers, "disclaimer")

    parsed: list[dict] = []
    invalid_rows = 0
    for row in rows:
        event_time = _event_time(_cell(row, date_col), _cell(row, time_col))
        vintage = _cell(row, vintage_col)
        price = number(_cell(row, price_col))
        volume = number(_cell(row, volume_col))
        if event_time is None or not vintage or price is None or price <= 0 or volume is None:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        symbol = f"NEZ_{vintage}"
        disclaimer = _cell(row, disclaimer_col)
        parsed.append(
            {
                "venue": "EEX",
                "inst_id": f"EEX:{symbol}:SALE:{event_time.date().isoformat()}",
                "instrument_id": f"EEX:{symbol}:SALE:{event_time.date().isoformat()}",
                "symbol": symbol,
                "name": f"German nEHS national emissions certificate {vintage} sale",
                "base": symbol,
                "quote": "EUR_PER_NEZ",
                "market_type": "sale_reference",
                "market_surface": "eex_german_nehs_sales_results",
                "asset_class": "national_emissions_certificate",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "sale_price": price,
                "sold_volume": volume,
                "transaction_count": int(number(_cell(row, trades_col)) or 0),
                "buyer_count": int(number(_cell(row, buyers_col)) or 0),
                "revenue_eur": number(_cell(row, revenue_col)),
                "vintage": int(vintage) if vintage.isdigit() else vintage,
                "event_date": event_time.date().isoformat(),
                "event_time_utc": event_time.isoformat(),
                "result_finality": "preliminary" if disclaimer else "final",
                "result_disclaimer": disclaimer or None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_event_result",
                "freshness_state": freshness_state,
                "freshness_basis": "official_sale_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "EEX German nEHS sales report",
                "source_url": source_url,
                "candidate_reject_reason": "sale_result_reference_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} data rows were invalid" if invalid_rows else ""
        raise EexNehsParseError(f"no usable nEHS sale result rows{detail}")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def datasource_auction_url(trade_date: str) -> str:
    query = urllib.parse.urlencode({"tradeDate": trade_date})
    return f"{DATASOURCE_API_ROOT}/getAuction/json?{query}"


def datasource_spot_url(trade_date: str) -> str:
    query = urllib.parse.urlencode(
        {
            "returnType": "trades",
            "commodity": "EMISSIONS",
            "tradeDate": trade_date,
            "root": "SEME,SEMA",
        }
    )
    return f"{DATASOURCE_API_ROOT}/getSpot/json?{query}"


def _api_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "results" not in payload:
        raise EexEuEtsParseError("DataSource JSON object with a results field was not found")
    result_groups = payload.get("results")
    if result_groups in (None, ""):
        return []
    if not isinstance(result_groups, list):
        result_groups = [result_groups]
    rows: list[dict[str, Any]] = []
    for group in result_groups:
        values = group.get("result") if isinstance(group, dict) and "result" in group else group
        if values in (None, ""):
            continue
        if not isinstance(values, list):
            values = [values]
        rows.extend(value for value in values if isinstance(value, dict))
    return rows


def _field_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _header_token(value))


def _row_value(row: dict[str, Any], *names: str) -> Any:
    normalized = {_field_token(key): value for key, value in row.items()}
    for name in names:
        token = _field_token(name)
        if token in normalized:
            return normalized[token]
    return None


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        try:
            return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(value))).date()
        except (OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _parse_time(value: Any) -> dt.time | None:
    if isinstance(value, dt.datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, dt.time):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        seconds = round((float(value) % 1.0) * 86400.0)
        seconds = min(max(0, seconds), 86399)
        return dt.time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    text = str(value or "").strip()
    for pattern in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    return None


def _eu_auction_time(date_value: Any, time_value: Any) -> dt.datetime | None:
    event_date = _parse_date(date_value)
    event_time = _parse_time(time_value) or dt.time(0, 0)
    if event_date is None:
        return None
    try:
        timezone = ZoneInfo("Europe/Berlin")
    except ZoneInfoNotFoundError:
        timezone = dt.timezone.utc
    local = dt.datetime.combine(event_date, event_time, tzinfo=timezone)
    return local.astimezone(dt.timezone.utc)


def _allowance_type(*values: Any) -> str:
    text = " ".join(str(value or "").upper() for value in values)
    if "EUAA" in text or re.search(r"\bSEMA\b", text) or re.search(r"\bFEAA\b", text):
        return "EUAA"
    return "EUA"


def _xlsx_rows(content: bytes) -> list[dict[int, Any]]:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise EexEuEtsParseError("empty XLSX response")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(content)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise EexEuEtsParseError(f"invalid XLSX archive: {exc}") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > 25_000_000:
            raise EexEuEtsParseError("expanded XLSX content exceeds 25000000 byte limit")
        worksheet_names = sorted(
            name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        if not worksheet_names:
            raise EexEuEtsParseError("XLSX worksheet was not found")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise EexEuEtsParseError(f"invalid XLSX shared strings: {exc}") from exc
            namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for item in root.findall(f"{namespace}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{namespace}t")))
        try:
            sheet = ET.fromstring(archive.read(worksheet_names[0]))
        except ET.ParseError as exc:
            raise EexEuEtsParseError(f"invalid XLSX worksheet: {exc}") from exc
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rows: list[dict[int, Any]] = []
    for row_node in sheet.iter(f"{namespace}row"):
        row: dict[int, Any] = {}
        for cell in row_node.findall(f"{namespace}c"):
            reference = str(cell.get("r") or "")
            match = re.match(r"([A-Z]+)", reference)
            if not match:
                continue
            column = 0
            for char in match.group(1):
                column = column * 26 + ord(char) - 64
            cell_type = cell.get("t")
            value_node = cell.find(f"{namespace}v")
            raw = value_node.text if value_node is not None else None
            if cell_type == "s" and raw is not None:
                try:
                    value: Any = shared[int(raw)]
                except (IndexError, ValueError) as exc:
                    raise EexEuEtsParseError("XLSX shared string index is invalid") from exc
            elif cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{namespace}t"))
            elif raw is None:
                value = None
            elif cell_type in {"str", "e"}:
                value = raw
            else:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
            row[column] = value
        if row:
            rows.append(row)
    return rows


def _auction_observation(
    row: dict[str, Any],
    *,
    event_time: dt.datetime,
    source_url: str,
    received_at: str | None,
    stale_after_hours: float,
    source_kind: str,
) -> dict[str, Any] | None:
    auction_name = str(_row_value(row, "AuctionName", "Auction Name") or "").strip()
    contract = str(_row_value(row, "Contract") or "").strip()
    status = str(_row_value(row, "Status") or "unknown").strip().lower()
    price = number(
        _row_value(row, "AuctionClearingPrice", "Auction Price EUR/tCO2", "Auction Price tCO2")
    )
    volume = number(_row_value(row, "AuctionVolume", "Auction Volume tCO2"))
    if not auction_name or price is None or price <= 0 or volume is None or volume <= 0:
        return None
    allowance = _allowance_type(auction_name, contract)
    freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
    total_bid_volume = number(
        _row_value(row, "TotalVolumeOfBidsSubmitted", "Total Amount of Bids")
    )
    cover_ratio = number(_row_value(row, "CoverRatio", "Cover Ratio"))
    if cover_ratio is None and total_bid_volume is not None:
        cover_ratio = round(total_bid_volume / volume, 6)
    zone = str(_row_value(row, "CountryRevenue", "Zone") or "").strip() or None
    mean_bid = number(_row_value(row, "Mean", "Mean EUR/tCO2", "Mean tCO2"))
    median_bid = number(_row_value(row, "Median", "Median EUR/tCO2", "Median tCO2"))
    country_revenues: dict[str, float] = {}
    for field_name, field_value in row.items():
        country_match = re.search(r"\(([A-Z]{2})\)", str(field_name).upper())
        parsed_revenue = number(str(field_value)) if field_value not in (None, "") else None
        if country_match and parsed_revenue is not None:
            country_revenues[country_match.group(1)] = parsed_revenue
    event_date = event_time.date().isoformat()
    return {
        "venue": "EEX",
        "inst_id": f"EEX:{allowance}:PRIMARY_AUCTION:{event_date}:{zone or contract or 'ALL'}",
        "instrument_id": f"EEX:{allowance}:PRIMARY_AUCTION:{event_date}:{zone or contract or 'ALL'}",
        "symbol": allowance,
        "name": auction_name,
        "base": allowance,
        "quote": "EUR_PER_TCO2",
        "market_type": "auction_reference",
        "market_surface": "eex_eu_ets_primary_auction_results",
        "asset_class": "emission_allowance",
        "allowance_type": allowance,
        "trade_type": "official_market_reference",
        "direction": "watch_only",
        "last": price,
        "auction_price": price,
        "auction_clearing_price": price,
        "minimum_bid": number(
            _row_value(row, "MinimumBid", "Minimum Bid EUR/tCO2", "Minimum Bid tCO2")
        ),
        "maximum_bid": number(
            _row_value(row, "MaximumBid", "Maximum Bid EUR/tCO2", "Maximum Bid tCO2")
        ),
        "mean_bid": mean_bid,
        "median_bid": median_bid,
        "clearing_price_vs_mean": round(price - mean_bid, 6) if mean_bid is not None else None,
        "clearing_price_vs_median": round(price - median_bid, 6) if median_bid is not None else None,
        "auction_volume": volume,
        "total_bid_volume": total_bid_volume,
        "total_volume_of_bids": total_bid_volume,
        "bid_volume_excess": total_bid_volume - volume if total_bid_volume is not None else None,
        "cover_ratio": cover_ratio,
        "bid_count": int(number(_row_value(row, "NumberOfBidsSubmitted", "Number of bids submitted")) or 0),
        "number_of_bids_submitted": int(
            number(_row_value(row, "NumberOfBidsSubmitted", "Number of bids submitted")) or 0
        ),
        "successful_bid_count": int(
            number(_row_value(row, "NumberOfSuccessfulBids", "Number of successful bids")) or 0
        ),
        "number_of_successful_bids": int(
            number(_row_value(row, "NumberOfSuccessfulBids", "Number of successful bids")) or 0
        ),
        "bidder_count": int(number(_row_value(row, "TotalNumberOfBidders", "Total Number of Bidders")) or 0),
        "total_number_of_bidders": int(
            number(_row_value(row, "TotalNumberOfBidders", "Total Number of Bidders")) or 0
        ),
        "successful_bidder_count": int(
            number(_row_value(row, "NumberOfSuccessfulBidders", "Number of Successful Bidders")) or 0
        ),
        "number_of_successful_bidders": int(
            number(_row_value(row, "NumberOfSuccessfulBidders", "Number of Successful Bidders")) or 0
        ),
        "average_number_of_bids_per_bidder": number(
            _row_value(row, "AverageNumberOfBidsPerBidder", "Average number of bids per bidder")
        ),
        "average_bid_size": number(_row_value(row, "AverageBidSize", "Average bid size")),
        "average_volume_bid_per_bidder": number(
            _row_value(row, "AverageVolumeBidPerBidder", "Average volume bid per bidder")
        ),
        "standard_deviation_bid_volume_per_bidder": number(
            _row_value(
                row,
                "StandardDeviationOfBidVolumePerBidder",
                "Standard deviation of bid volume per bidder",
            )
        ),
        "average_volume_won_per_bidder": number(
            _row_value(row, "AverageVolumeWonPerBidder", "Average volume won per bidder")
        ),
        "standard_deviation_volume_won_per_bidder": number(
            _row_value(
                row,
                "StandardDeviationOfVolumeWonPerBidder",
                "Standard deviation of volume won per bidder",
            )
        ),
        "total_revenue_eur": number(_row_value(row, "TotalRevenue", "Total Revenue EUR", "Total Revenue")),
        "country_revenues_eur": country_revenues,
        "unit_of_prices": str(_row_value(row, "UnitOfPrices") or "EUR").strip(),
        "unit_of_volumes": "tCO2",
        "zone": zone,
        "contract": contract or None,
        "auction_status": status,
        "event_date": event_date,
        "event_time_utc": event_time.isoformat(),
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_event_result",
        "freshness_state": freshness_state,
        "freshness_basis": "official_auction_result_timestamp_europe_berlin",
        "freshness_age_seconds": freshness_age,
        "session_status": "closed" if status == "successful" else status,
        "observed_at": event_time.isoformat(),
        "fetched_at": received_at or utc_now(),
        "source_record_type": source_kind,
        "price_source": "EEX EU ETS primary auction result",
        "source_url": source_url,
        "candidate_reject_reason": "auction_result_reference_not_executable_quote",
    }


def parse_eex_eu_ets_auction(
    payload: Any,
    *,
    trade_date: str,
    source_url: str | None = None,
    received_at: str | None = None,
    stale_after_hours: float = 72.0,
    limit: int = 100,
) -> list[dict]:
    """Normalize documented DataSource getAuction JSON rows for EUA and EUAA."""

    event_date = _parse_date(trade_date)
    if event_date is None:
        raise EexEuEtsParseError(f"invalid getAuction trade date: {trade_date}")
    parsed: list[dict] = []
    invalid_rows = 0
    for row in _api_rows(payload):
        event_time = _eu_auction_time(event_date, _row_value(row, "Time"))
        observation = (
            _auction_observation(
                row,
                event_time=event_time,
                source_url=source_url or datasource_auction_url(trade_date),
                received_at=received_at,
                stale_after_hours=stale_after_hours,
                source_kind="datasource_getAuction",
            )
            if event_time
            else None
        )
        if observation is None:
            invalid_rows += 1
            continue
        parsed.append(observation)
    if not parsed and invalid_rows:
        raise EexEuEtsParseError(f"no usable getAuction rows; {invalid_rows} rows were invalid")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def parse_eex_eua_auction_workbook(
    content: bytes,
    *,
    source_url: str = EUA_AUCTION_REPORT_URL,
    received_at: str | None = None,
    stale_after_hours: float = 72.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize EEX's public current-year primary-auction XLSX report."""

    rows = _xlsx_rows(content)
    header_position: int | None = None
    headers: dict[int, str] = {}
    for position, raw_row in enumerate(rows):
        candidate = {column: str(value or "").strip() for column, value in raw_row.items()}
        tokens = {_field_token(value) for value in candidate.values()}
        if "date" in tokens and "auctionname" in tokens and any("auctionprice" in token for token in tokens):
            header_position = position
            headers = candidate
            break
    if header_position is None:
        raise EexEuEtsParseError("XLSX auction header with Date, Auction Name, and Auction Price was not found")
    parsed: list[dict] = []
    invalid_rows = 0
    for raw_row in rows[header_position + 1 :]:
        row = {headers[column]: value for column, value in raw_row.items() if column in headers}
        event_time = _eu_auction_time(_row_value(row, "Date"), _row_value(row, "Time"))
        observation = (
            _auction_observation(
                row,
                event_time=event_time,
                source_url=source_url,
                received_at=received_at,
                stale_after_hours=stale_after_hours,
                source_kind="public_xlsx_market_data_file",
            )
            if event_time
            else None
        )
        if observation is None:
            if any(value not in (None, "") for value in raw_row.values()):
                invalid_rows += 1
            continue
        parsed.append(observation)
    if not parsed:
        detail = f"; {invalid_rows} data rows were invalid" if invalid_rows else ""
        raise EexEuEtsParseError(f"no usable public XLSX auction rows{detail}")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def parse_eex_emissions_spot(
    payload: Any,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_hours: float = 48.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize documented DataSource getSpot emission trade rows."""

    parsed: list[dict] = []
    invalid_rows = 0
    for row in _api_rows(payload):
        root = str(_row_value(row, "Root") or "").strip().upper()
        product = str(_row_value(row, "Product") or "").strip()
        long_name = str(_row_value(row, "LongName", "Long Name") or "").strip()
        price = number(_row_value(row, "Price"))
        timestamp_value = _row_value(row, "TradeTimestamp", "TradeTimeStamp", "TradedTimeStamp")
        try:
            event_time = dt.datetime.fromisoformat(str(timestamp_value or "").replace("Z", "+00:00"))
        except ValueError:
            event_time = None
        if event_time is not None and event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=dt.timezone.utc)
        if not root or not long_name or price is None or price <= 0 or event_time is None:
            invalid_rows += 1
            continue
        allowance = _allowance_type(root, product, long_name)
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        trade_id = str(_row_value(row, "TradeID") or event_time.isoformat())
        parsed.append(
            {
                "venue": "EEX",
                "inst_id": f"EEX:{allowance}:SPOT:{trade_id}",
                "instrument_id": f"EEX:{allowance}:SPOT:{trade_id}",
                "symbol": allowance,
                "name": long_name,
                "base": allowance,
                "quote": "EUR_PER_TCO2",
                "market_type": "spot_trade_reference",
                "market_surface": "eex_eu_ets_secondary_spot_trades",
                "asset_class": "emission_allowance",
                "allowance_type": allowance,
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "trade_price": price,
                "traded_volume": number(_row_value(row, "TradedVolume")),
                "market_area": str(_row_value(row, "MarketArea") or "").strip() or None,
                "unit_of_prices": str(_row_value(row, "UnitOfPrices") or "EUR").strip(),
                "unit_of_volumes": str(_row_value(row, "UnitOfVolumes") or "tCO2").strip(),
                "reported_trade_type": str(_row_value(row, "TradedType") or "").strip() or None,
                "root": root,
                "product": product or None,
                "trade_id": trade_id,
                "valid_trade": str(_row_value(row, "ValidTrade") or "").strip() or None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_reported_trade",
                "freshness_state": freshness_state,
                "freshness_basis": "official_trade_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.astimezone(dt.timezone.utc).isoformat(),
                "fetched_at": received_at or utc_now(),
                "source_record_type": "datasource_getSpot",
                "price_source": "EEX EU ETS secondary spot trade",
                "source_url": source_url,
                "candidate_reject_reason": "reported_spot_trade_not_executable_quote",
            }
        )
    if not parsed and invalid_rows:
        raise EexEuEtsParseError(f"no usable getSpot rows; {invalid_rows} rows were invalid")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def _source_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
    }


def _failure_observation(
    source_url: str,
    result: dict[str, Any],
    surface: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("EEX", source_url, evidence, surface)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": "public_reference_parser_failure"
            if parser_error
            else "public_reference_source_unavailable",
        }
    )
    return observation


class EexGermanNehsAdapter:
    info = AdapterInfo(
        adapter_id="eex_german_nehs_public",
        venue="EEX",
        market_type="emissions_auction",
        source="EEX official German nEHS public CSV reports",
        capabilities=(
            "auction_results",
            "sale_results",
            "settlement_reference",
            "volume",
            "participant_statistics",
            "source_health",
        ),
        aliases=(
            "european energy exchange",
            "eex",
            "german nehs",
            "nez",
            "national emissions certificates",
        ),
        docs_url=DATA_PAGE_URL,
        runtime_entrypoint="adapters.venues.european_energy_exchange_eex.EexGermanNehsAdapter",
        quote_assets=("EUR_PER_NEZ",),
        default_cache_minutes=30,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(self.info.adapter_id, {})
        timeout = int(cfg.get("timeout_seconds", 15))
        limit = max(1, int(cfg.get("max_rows_per_report", 250)))
        stale_after_hours = float(cfg.get("stale_after_hours", 192.0))
        sources = (
            ("auction", AUCTION_URL, parse_eex_nehs_auction, "eex_german_nehs_auction_results"),
            ("sales", SALES_URL, parse_eex_nehs_sales, "eex_german_nehs_sales_results"),
        )
        observations: list[dict] = []
        parser_failures: list[dict[str, str]] = []
        source_health: dict[str, dict[str, Any]] = {}
        usable_reports = 0

        for report_type, source_url, parser, surface in sources:
            result = fetch_text(source_url, timeout)
            source_health[report_type] = _source_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                rows = parser(
                    result.get("text") or "",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                )
            except (csv.Error, EexNehsParseError, TypeError, ValueError) as exc:
                message = f"EEX {report_type} parser failed: {exc}"
                parser_failures.append(
                    {"report_type": report_type, "source_url": source_url, "error": message[:300]}
                )
                observations.append(_failure_observation(source_url, result, surface, message[:300]))
                continue
            observations.extend(rows)
            usable_reports += 1

        fetch_statuses = [item["fetch_status"] for item in source_health.values()]
        if usable_reports == len(sources) and not parser_failures:
            source_status = "reachable"
        elif usable_reports:
            source_status = "degraded"
        elif parser_failures:
            source_status = "degraded"
        elif "blocked" in fetch_statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = {str(row.get("freshness_state")) for row in observations}
        freshness_state = "fresh" if "fresh" in freshness_states else "stale" if "stale" in freshness_states else "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "source_status": source_status,
                "source_urls": [AUCTION_URL, SALES_URL],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "session_state": "closed_event_results",
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "auction_observation_count": sum(
                    1 for row in observations if row.get("market_surface") == "eex_german_nehs_auction_results"
                ),
                "sales_observation_count": sum(
                    1 for row in observations if row.get("market_surface") == "eex_german_nehs_sales_results"
                ),
                "paper_only": True,
            },
        )


class EexEuaPrimaryAuctionSpotAdapter:
    info = AdapterInfo(
        adapter_id="eex_eua_primary_auction_spot_public",
        venue="EEX",
        market_type="eu_ets_emissions",
        source="EEX official EU ETS primary-auction and emissions spot data",
        capabilities=(
            "auction_results",
            "secondary_spot_trades",
            "datasource_getAuction",
            "datasource_getSpot",
            "public_market_data_file",
            "volume",
            "participant_statistics",
            "source_health",
        ),
        aliases=(
            "european energy exchange",
            "eex",
            "eua",
            "euaa",
            "eu ets primary auction",
            "emissions spot",
        ),
        docs_url=DATASOURCE_DOCS_URL,
        runtime_entrypoint=(
            "adapters.venues.european_energy_exchange_eex.EexEuaPrimaryAuctionSpotAdapter"
        ),
        quote_assets=("EUR_PER_TCO2",),
        default_cache_minutes=15,
    )

    @staticmethod
    def _config(settings: dict | None, adapter_id: str) -> dict[str, Any]:
        root = ((settings or {}).get("public_market_adapters") or {})
        nested = (root.get("adapters") or {}).get(adapter_id) or {}
        direct = root.get(adapter_id) or {}
        return {**nested, **direct}

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = self._config(settings, self.info.adapter_id)
        timeout = int(cfg.get("timeout_seconds", 15))
        limit = max(1, int(cfg.get("max_rows_per_source", 250)))
        stale_after_hours = float(cfg.get("stale_after_hours", 72.0))
        trade_date = str(cfg.get("trade_date") or dt.datetime.now(dt.timezone.utc).date().isoformat())
        parsed_trade_date = _parse_date(trade_date)
        if parsed_trade_date is None:
            raise ValueError(f"invalid EEX trade_date setting: {trade_date}")
        report_year = int(cfg.get("report_year", parsed_trade_date.year))
        auction_api_url = datasource_auction_url(trade_date)
        spot_api_url = datasource_spot_url(trade_date)
        report_url = eua_auction_report_url(report_year)
        observations: list[dict] = []
        parser_failures: list[dict[str, str]] = []
        source_health: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        api_sources = (
            (
                "auction_api",
                auction_api_url,
                "eex_eu_ets_primary_auction_results",
                lambda payload, result: parse_eex_eu_ets_auction(
                    payload,
                    trade_date=trade_date,
                    source_url=auction_api_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                ),
            ),
            (
                "spot_api",
                spot_api_url,
                "eex_eu_ets_secondary_spot_trades",
                lambda payload, result: parse_eex_emissions_spot(
                    payload,
                    source_url=spot_api_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                ),
            ),
        )
        for source_name, source_url, surface, parser in api_sources:
            result = fetch_text(source_url, timeout)
            source_health[source_name] = _source_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                payload = parse_json(str(result.get("text") or ""))
                rows = parser(payload, result)
            except (EexEuEtsParseError, TypeError, ValueError) as exc:
                message = f"EEX {source_name} parser failed: {exc}"
                parser_failures.append(
                    {"source": source_name, "source_url": source_url, "error": message[:300]}
                )
                observations.append(_failure_observation(source_url, result, surface, message[:300]))
                continue
            observations.extend(rows)
            usable_sources += 1

        workbook_result = fetch_bytes(report_url, timeout)
        source_health["auction_file"] = _source_evidence(workbook_result, report_url)
        if not workbook_result.get("ok"):
            observations.append(
                _failure_observation(
                    report_url,
                    workbook_result,
                    "eex_eu_ets_primary_auction_results",
                )
            )
        else:
            try:
                observations.extend(
                    parse_eex_eua_auction_workbook(
                        workbook_result.get("content") or b"",
                        source_url=report_url,
                        received_at=workbook_result.get("received_at"),
                        stale_after_hours=stale_after_hours,
                        limit=limit,
                    )
                )
                usable_sources += 1
            except (EexEuEtsParseError, TypeError, ValueError, zipfile.BadZipFile) as exc:
                message = f"EEX auction_file parser failed: {exc}"
                parser_failures.append(
                    {"source": "auction_file", "source_url": report_url, "error": message[:300]}
                )
                observations.append(
                    _failure_observation(
                        report_url,
                        workbook_result,
                        "eex_eu_ets_primary_auction_results",
                        message[:300],
                    )
                )

        fetch_statuses = [item["fetch_status"] for item in source_health.values()]
        if usable_sources == len(source_health) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in fetch_statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = {str(row.get("freshness_state")) for row in real_observations}
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "source_status": source_status,
                "source_urls": [auction_api_url, spot_api_url, report_url],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "session_state": "closed_results_watch_only",
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "auction_observation_count": sum(
                    1
                    for row in real_observations
                    if row.get("market_surface") == "eex_eu_ets_primary_auction_results"
                ),
                "spot_observation_count": sum(
                    1
                    for row in real_observations
                    if row.get("market_surface") == "eex_eu_ets_secondary_spot_trades"
                ),
                "api_auth_mode": "anonymous_only",
                "paper_only": True,
            },
        )


class EexUkaFuturesOptionsAdapter:
    """Public EEX UKA contract catalog with no order or quote capability."""

    info = AdapterInfo(
        adapter_id="eex_uka_futures_options_public",
        venue="EEX",
        market_type="emissions_derivatives",
        source="EEX official UK ETS UKA Futures and Options contract specifications",
        capabilities=(
            "public_market_data",
            "contract_catalog",
            "contract_identity",
            "emission_allowance_derivatives",
            "futures",
            "options",
            "source_health",
        ),
        aliases=(
            "european energy exchange",
            "eex",
            "uk ets",
            "uka",
            "uk emission allowance",
            "uka futures",
            "uka options",
        ),
        docs_url=UK_ETS_PAGE_URL,
        runtime_entrypoint="adapters.venues.european_energy_exchange_eex.EexUkaFuturesOptionsAdapter",
        quote_assets=("GBP_PER_UKA",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = EexEuaPrimaryAuctionSpotAdapter._config(settings, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or UK_ETS_PAGE_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []
        source_health = {"contract_specification": _source_evidence(result, source_url)}

        if not result.get("ok"):
            observations = [
                _failure_observation(
                    source_url,
                    result,
                    "eex_uk_ets_uka_futures_options",
                )
            ]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_eex_uka_futures_options(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
                freshness_state = "fresh"
                session_state = "reference_only"
            except (EexUkaParseError, TypeError, ValueError) as exc:
                message = f"EEX UKA contract-specification parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [
                    _failure_observation(
                        source_url,
                        result,
                        "eex_uk_ets_uka_futures_options",
                        message,
                    )
                ]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1400,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "futures_observation_count": sum(
                    1 for row in real_observations if row.get("market_type") == "futures_catalog"
                ),
                "options_observation_count": sum(
                    1 for row in real_observations if row.get("market_type") == "options_catalog"
                ),
                "capability_gap": "public_quotes_and_order_book_not_available",
                "paper_only": True,
            },
        )


register_adapter(EexGermanNehsAdapter())
register_adapter(EexEuaPrimaryAuctionSpotAdapter())
register_adapter(EexUkaFuturesOptionsAdapter())
