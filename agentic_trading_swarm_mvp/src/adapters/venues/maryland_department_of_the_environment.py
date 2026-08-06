"""Maryland Water Quality Trading public registry and market-board adapter.

MDE publishes a public certified-credit workbook, a public market-board page,
and summary pricing averages for nitrogen, phosphorus, and sediment credits.
These are research surfaces rather than anonymous executable quotes, so this
adapter normalizes them as watch-only observations and source-health evidence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import (
    fetch_bytes,
    fetch_text,
    health_observation,
    number,
    slug,
    utc_now,
)
from scan_batch import ScanBatch


PROGRAM_URL = "https://mde.maryland.gov/programs/water/WQT/Pages/WQT_Registry_Market.aspx"
MARKET_BOARD_URL = "https://mde.maryland.gov/programs/water/WQT/Pages/WQT-MarketBoard.aspx"
REGISTRY_WORKBOOK_URL = "https://mde.maryland.gov/programs/water/WQT/Documents/MDE_REGISTER_WEB.xlsx"
PURCHASING_FAQ_URL = (
    "https://mde.maryland.gov/programs/Water/WQT/Documents/Guidance%20PDFs/Purchasing%20FAQ.pdf"
)

VENUE = "MDE_MARYLAND_WQT"
PRICING_SURFACE = "maryland_water_quality_trading_market_pricing"
MARKET_BOARD_SURFACE = "maryland_water_quality_trading_market_board"
GENERATED_SURFACE = "maryland_water_quality_trading_certified_credits_generated"
RESERVE_SURFACE = "maryland_water_quality_trading_reserve_pool"
TRADES_SURFACE = "maryland_water_quality_trading_registered_trades"


class MarylandWaterQualityTradingParseError(ValueError):
    """Raised when a reachable MDE public source changes schema."""


class _BreakAwareTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in {"script", "style", "noscript"}:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if lower == "table":
            self._table = []
        elif self._table is not None and lower == "tr":
            self._row = []
        elif self._row is not None and lower in {"td", "th"}:
            self._cell = []
        elif self._cell is not None and lower == "br":
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in {"script", "style", "noscript"} and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if lower in {"td", "th"} and self._cell is not None and self._row is not None:
            lines = [part.strip() for part in "".join(self._cell).splitlines()]
            self._row.append("\n".join(part for part in lines if part))
            self._cell = None
        elif lower == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif lower == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._suppressed_depth:
            self._cell.append(data)


def _html_tables(document: str) -> list[list[list[str]]]:
    if not isinstance(document, str) or not document.strip():
        raise MarylandWaterQualityTradingParseError("official HTML response is empty")
    parser = _BreakAwareTableParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain source-health evidence.
        raise MarylandWaterQualityTradingParseError(f"invalid official HTML response: {exc}") from exc
    if not parser.tables:
        raise MarylandWaterQualityTradingParseError("official HTML response contained no tables")
    return parser.tables


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarylandWaterQualityTradingParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _field_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _column_index(ref: str) -> int | None:
    letters = "".join(char for char in str(ref or "") if char.isalpha())
    if not letters:
        return None
    value = 0
    for char in letters:
        value = value * 26 + ord(char.upper()) - 64
    return value - 1


def _xlsx_sheets(content: bytes) -> dict[str, list[list[Any]]]:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise MarylandWaterQualityTradingParseError("registry workbook response is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(content)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise MarylandWaterQualityTradingParseError(f"invalid registry workbook: {exc}") from exc
    with archive:
        if sum(item.file_size for item in archive.infolist()) > 25_000_000:
            raise MarylandWaterQualityTradingParseError(
                "expanded registry workbook content exceeds 25000000 byte limit"
            )
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        rel_namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        try:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except ET.ParseError as exc:
            raise MarylandWaterQualityTradingParseError(f"invalid registry workbook XML: {exc}") from exc
        rels = {str(item.attrib.get("Id")): str(item.attrib.get("Target")) for item in rels_root}
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise MarylandWaterQualityTradingParseError(
                    f"invalid registry workbook shared strings: {exc}"
                ) from exc
            shared = [
                "".join(node.text or "" for node in item.iter(f"{namespace}t"))
                for item in shared_root.findall(f"{namespace}si")
            ]

        sheets: dict[str, list[list[Any]]] = {}
        for sheet_node in workbook_root.findall(f"{namespace}sheets/{namespace}sheet"):
            name = str(sheet_node.attrib.get("name") or "").strip()
            rel_id = str(sheet_node.attrib.get(f"{rel_namespace}id") or "").strip()
            target = rels.get(rel_id)
            if not name or not target:
                continue
            worksheet_path = "xl/" + target.removeprefix("/")
            try:
                worksheet_root = ET.fromstring(archive.read(worksheet_path))
            except ET.ParseError as exc:
                raise MarylandWaterQualityTradingParseError(
                    f"invalid registry workbook worksheet {name}: {exc}"
                ) from exc
            rows: list[list[Any]] = []
            for row_node in worksheet_root.findall(f".//{namespace}row"):
                raw: dict[int, Any] = {}
                max_index = -1
                for cell in row_node.findall(f"{namespace}c"):
                    index = _column_index(str(cell.attrib.get("r") or ""))
                    if index is None:
                        continue
                    max_index = max(max_index, index)
                    cell_type = str(cell.attrib.get("t") or "")
                    raw_node = cell.find(f"{namespace}v")
                    raw_value = raw_node.text if raw_node is not None else None
                    if cell_type == "s" and raw_value is not None:
                        try:
                            value: Any = shared[int(raw_value)]
                        except (IndexError, ValueError) as exc:
                            raise MarylandWaterQualityTradingParseError(
                                "registry workbook shared-string index is invalid"
                            ) from exc
                    elif cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{namespace}t"))
                    elif raw_value is None:
                        value = ""
                    else:
                        value = raw_value
                    raw[index] = value
                if raw:
                    rows.append([raw.get(index, "") for index in range(max_index + 1)])
            if rows:
                sheets[name] = rows
    if not sheets:
        raise MarylandWaterQualityTradingParseError("registry workbook contained no worksheets")
    return sheets


def _sheet_records(
    sheets: dict[str, list[list[Any]]],
    sheet_name: str,
    required: set[str],
) -> list[dict[str, Any]]:
    rows = sheets.get(sheet_name)
    if not rows:
        raise MarylandWaterQualityTradingParseError(f"registry workbook sheet {sheet_name} was not found")
    for position, row in enumerate(rows):
        headers = [_field_token(value) for value in row]
        if not required.issubset(set(headers)):
            continue
        records: list[dict[str, Any]] = []
        for raw_row in rows[position + 1 :]:
            if not any(str(value or "").strip() for value in raw_row):
                continue
            records.append(
                {
                    header: raw_row[index] if index < len(raw_row) else ""
                    for index, header in enumerate(headers)
                    if header
                }
            )
        return records
    raise MarylandWaterQualityTradingParseError(
        f"registry workbook sheet {sheet_name} is missing required fields: {', '.join(sorted(required))}"
    )


def _excel_date(value: Any) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        try:
            return dt.date(1899, 12, 30) + dt.timedelta(days=numeric)
        except (OverflowError, ValueError):
            return None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _stable_id(*values: Any) -> str:
    digest = hashlib.sha256("|".join(str(value or "").strip() for value in values).encode("utf-8")).hexdigest()
    return digest[:16].upper()


def _segmentsheds(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[,;/]", str(value or "")) if part.strip()]


def _pollutant(value: Any) -> tuple[str, str, str]:
    token = _field_token(value)
    if token in {"nitrogen", "tn"}:
        return "Nitrogen", "N", "MD_WQT_NITROGEN_REDUCTION_CREDIT"
    if token in {"phosphorus", "tp"}:
        return "Phosphorus", "P", "MD_WQT_PHOSPHORUS_REDUCTION_CREDIT"
    if token in {"sediment", "tss"}:
        return "Sediment", "S", "MD_WQT_SEDIMENT_REDUCTION_CREDIT"
    raise MarylandWaterQualityTradingParseError(f"unsupported pollutant label: {value}")


def parse_maryland_wqt_market_pricing(
    document: str,
    *,
    source_url: str = PROGRAM_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize public low/middle/high price averages by pollutant."""

    table = next(
        (
            candidate
            for candidate in _html_tables(document)
            if candidate
            and any("nitrogen" in str(cell).lower() for cell in candidate[0])
            and any("phosphorus" in str(cell).lower() for cell in candidate[0])
            and any("sediment" in str(cell).lower() for cell in candidate[0])
        ),
        None,
    )
    if table is None or len(table) < 4:
        raise MarylandWaterQualityTradingParseError("market pricing table was not found")
    fetched_at = _received_time(received_at)
    header = [_field_token(value) for value in table[0]]
    low_row = {_field_token(table[1][0]): table[1]}
    middle_row = {_field_token(table[2][0]): table[2]}
    high_row = {_field_token(table[3][0]): table[3]}
    if "low" not in low_row or "middle" not in middle_row or "high" not in high_row:
        raise MarylandWaterQualityTradingParseError("market pricing low/middle/high rows were not found")
    rows: list[dict[str, Any]] = []
    for label in ("nitrogen", "phosphorus", "sediment"):
        try:
            column = header.index(next(item for item in header if label in item))
        except StopIteration as exc:
            raise MarylandWaterQualityTradingParseError(
                f"market pricing column for {label} was not found"
            ) from exc
        pollutant_name, pollutant_code, base_asset = _pollutant(label)
        low = number(table[1][column])
        middle = number(table[2][column])
        high = number(table[3][column])
        if low is None or middle is None or high is None:
            raise MarylandWaterQualityTradingParseError(
                f"market pricing values for {pollutant_name} are not numeric"
            )
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:PRICE_AVERAGE:{pollutant_code}",
                "instrument_id": f"{VENUE}:PRICE_AVERAGE:{pollutant_code}",
                "symbol": f"MD_WQT_{pollutant_code}_PRICE_AVG",
                "name": f"Maryland WQT {pollutant_name} public average pricing",
                "base": base_asset,
                "quote": "USD_PER_LB_REDUCTION",
                "market_type": "price_reference",
                "market_surface": PRICING_SURFACE,
                "asset_class": "water_quality_trading_credit",
                "trade_type": "official_price_average_reference",
                "direction": "watch_only",
                "last": middle,
                "price_available": True,
                "pollutant": pollutant_name,
                "price_low_usd_per_lb": low,
                "price_middle_usd_per_lb": middle,
                "price_high_usd_per_lb": high,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_market_pricing_summary",
                "freshness_state": "fresh",
                "freshness_basis": "public_market_pricing_page_fetch",
                "freshness_age_seconds": 0.0,
                "session_status": "reference_only",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Maryland Water Quality Trading market pricing page",
                "source_url": source_url,
                "source_market_board_url": MARKET_BOARD_URL,
                "candidate_reject_reason": "public_average_pricing_reference_not_order_routable",
            }
        )
    return rows


def _market_board_quantities(value: Any) -> dict[str, float]:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    parsed: dict[str, float] = {}
    for code, amount in re.findall(
        r"\b(TN|TP|TSS)\s+Credits\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    ):
        qty = number(amount)
        if qty is not None:
            parsed[code.upper()] = qty
    return parsed


def parse_maryland_wqt_market_board(
    document: str,
    *,
    source_url: str = MARKET_BOARD_URL,
    received_at: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Normalize market-board buy/sell listings by pollutant without direct contacts."""

    required = {
        "ad_type",
        "contact_info",
        "segmentshed",
        "credit_year_needed",
        "credits_needed_available",
    }
    table = next(
        (
            candidate
            for candidate in _html_tables(document)
            if candidate and required.issubset({_field_token(value) for value in candidate[0]})
        ),
        None,
    )
    if table is None:
        raise MarylandWaterQualityTradingParseError("market board table was not found")
    fetched_at = _received_time(received_at)
    header = [_field_token(value) for value in table[0]]
    rows: list[dict[str, Any]] = []
    invalid_rows = 0
    for raw_row in table[1:]:
        record = {
            header[index]: raw_row[index] if index < len(raw_row) else ""
            for index in range(len(header))
            if header[index]
        }
        ad_type = str(record.get("ad_type") or "").strip()
        segmentshed = str(record.get("segmentshed") or "").strip()
        vintage = str(record.get("credit_year_needed") or "").strip()
        contact_lines = [part.strip() for part in str(record.get("contact_info") or "").splitlines() if part.strip()]
        quantities = _market_board_quantities(record.get("credits_needed_available"))
        if not ad_type or not segmentshed or not vintage or not quantities:
            invalid_rows += 1
            continue
        listing_side = "for_sale" if ad_type.casefold() == "for sale" else "wanted"
        session_status = "seller_listing_active" if listing_side == "for_sale" else "buyer_interest_active"
        entity = contact_lines[1] if len(contact_lines) >= 2 else contact_lines[0] if contact_lines else ""
        role = contact_lines[2] if len(contact_lines) >= 3 else None
        for code, qty in quantities.items():
            pollutant_name, pollutant_code, base_asset = _pollutant(code)
            identity = _stable_id(ad_type, segmentshed, vintage, entity, pollutant_code, qty)
            rows.append(
                {
                    "venue": VENUE,
                    "inst_id": f"{VENUE}:MARKET_BOARD:{identity}:{pollutant_code}",
                    "instrument_id": f"{VENUE}:MARKET_BOARD:{identity}:{pollutant_code}",
                    "symbol": f"MD_WQT_{pollutant_code}_{listing_side.upper()}",
                    "name": f"Maryland WQT {ad_type} listing for {pollutant_name}",
                    "base": base_asset,
                    "quote": "LB_REDUCTION",
                    "market_type": "seller_listing" if listing_side == "for_sale" else "buyer_interest",
                    "market_surface": MARKET_BOARD_SURFACE,
                    "asset_class": "water_quality_trading_credit",
                    "trade_type": "official_market_board_listing",
                    "direction": "watch_only",
                    "last": qty,
                    "price_available": False,
                    "pollutant": pollutant_name,
                    "listing_side": listing_side,
                    "segmentshed": segmentshed,
                    "segmentsheds": _segmentsheds(segmentshed),
                    "vintage_year": int(vintage) if vintage.isdigit() else vintage,
                    "credits_listed_or_needed": qty,
                    "listing_entity": entity or None,
                    "listing_role": role,
                    "source_contact_present": bool(contact_lines),
                    "direct_contact_channels_omitted": True,
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_market_board_listing",
                    "freshness_state": "fresh",
                    "freshness_basis": "public_market_board_fetch_timestamp",
                    "freshness_age_seconds": 0.0,
                    "session_status": session_status,
                    "observed_at": fetched_at.isoformat(),
                    "fetched_at": fetched_at.isoformat(),
                    "price_source": "Maryland Water Quality Trading Market Board",
                    "source_url": source_url,
                    "source_program_url": PROGRAM_URL,
                    "candidate_reject_reason": "public_market_board_listing_not_order_routable",
                }
            )
    if not rows:
        detail = f"; {invalid_rows} board rows were invalid" if invalid_rows else ""
        raise MarylandWaterQualityTradingParseError(f"no usable market-board rows{detail}")
    return rows[: max(1, int(limit))]


def parse_maryland_wqt_registry_workbook(
    content: bytes,
    *,
    source_url: str = REGISTRY_WORKBOOK_URL,
    received_at: str | None = None,
    limit_per_sheet: int = 10_000,
) -> list[dict[str, Any]]:
    """Normalize certified credits, reserve-pool rows, and registered trades."""

    fetched_at = _received_time(received_at)
    sheets = _xlsx_sheets(content)
    rows: list[dict[str, Any]] = []

    generated_records = _sheet_records(
        sheets,
        "Credits_Generated",
        {
            "credit_status",
            "credits_remaining",
            "current_credit_ids",
            "generator",
            "credit_sector",
            "watershed",
            "vintage",
            "credit_type",
            "total_credits_certified",
            "date_certified",
        },
    )
    invalid_generated = 0
    for record in generated_records[: max(1, int(limit_per_sheet))]:
        pollutant_name, pollutant_code, base_asset = _pollutant(record.get("credit_type"))
        credits_remaining = number(record.get("credits_remaining"))
        total_certified = number(record.get("total_credits_certified"))
        credit_ids = str(record.get("current_credit_ids") or "").strip()
        generator = str(record.get("generator") or "").strip()
        if credits_remaining is None or total_certified is None or not credit_ids or not generator:
            invalid_generated += 1
            continue
        date_certified = _excel_date(record.get("date_certified"))
        credit_status = str(record.get("credit_status") or "").strip() or "Unknown"
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:GENERATED:{_stable_id(credit_ids, generator, pollutant_code)}",
                "instrument_id": f"{VENUE}:GENERATED:{_stable_id(credit_ids, generator, pollutant_code)}",
                "symbol": f"MD_WQT_{pollutant_code}_CERTIFIED",
                "name": f"Maryland certified {pollutant_name} credits from {generator}",
                "base": base_asset,
                "quote": "LB_REDUCTION",
                "market_type": "certified_credit_registry",
                "market_surface": GENERATED_SURFACE,
                "asset_class": "water_quality_trading_credit",
                "trade_type": "official_certified_credit_registry",
                "direction": "watch_only",
                "last": credits_remaining,
                "price_available": False,
                "pollutant": pollutant_name,
                "credit_status": credit_status,
                "credits_remaining": credits_remaining,
                "total_credits_certified": total_certified,
                "current_credit_ids": credit_ids,
                "generator": generator,
                "credit_sector": str(record.get("credit_sector") or "").strip() or None,
                "watershed": str(record.get("watershed") or "").strip() or None,
                "vintage_year": int(str(record.get("vintage") or "").strip())
                if str(record.get("vintage") or "").strip().isdigit()
                else str(record.get("vintage") or "").strip() or None,
                "date_certified": date_certified.isoformat() if date_certified else None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_certified_credit_registry",
                "freshness_state": "fresh",
                "freshness_basis": "public_registry_workbook_fetch_timestamp",
                "freshness_age_seconds": 0.0,
                "session_status": "registry_available"
                if credit_status.casefold() == "available"
                else "registry_traded"
                if credit_status.casefold() == "traded"
                else "registry_snapshot",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Maryland WQT certified credits workbook",
                "source_url": source_url,
                "source_sheet": "Credits_Generated",
                "source_program_url": PROGRAM_URL,
                "candidate_reject_reason": "official_certified_credit_registry_not_order_routable",
            }
        )

    reserve_records = _sheet_records(
        sheets,
        "MD_Reserve",
        {
            "credit_ids",
            "generator",
            "county",
            "watershed",
            "vintage",
            "credit_type",
            "credits",
            "date_certified",
            "credit_status",
        },
    )
    invalid_reserve = 0
    for record in reserve_records[: max(1, int(limit_per_sheet))]:
        pollutant_name, pollutant_code, base_asset = _pollutant(record.get("credit_type"))
        reserve_credits = number(record.get("credits"))
        credit_ids = str(record.get("credit_ids") or "").strip()
        generator = str(record.get("generator") or "").strip()
        if reserve_credits is None or not credit_ids or not generator:
            invalid_reserve += 1
            continue
        date_certified = _excel_date(record.get("date_certified"))
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:RESERVE:{_stable_id(credit_ids, generator, pollutant_code)}",
                "instrument_id": f"{VENUE}:RESERVE:{_stable_id(credit_ids, generator, pollutant_code)}",
                "symbol": f"MD_WQT_{pollutant_code}_RESERVE",
                "name": f"Maryland reserve-pool {pollutant_name} credits from {generator}",
                "base": base_asset,
                "quote": "LB_REDUCTION",
                "market_type": "reserve_pool_registry",
                "market_surface": RESERVE_SURFACE,
                "asset_class": "water_quality_trading_credit",
                "trade_type": "official_reserve_pool_registry",
                "direction": "watch_only",
                "last": reserve_credits,
                "price_available": False,
                "pollutant": pollutant_name,
                "reserve_credits": reserve_credits,
                "credit_ids": credit_ids,
                "generator": generator,
                "county": str(record.get("county") or "").strip() or None,
                "watershed": str(record.get("watershed") or "").strip() or None,
                "vintage_year": int(str(record.get("vintage") or "").strip())
                if str(record.get("vintage") or "").strip().isdigit()
                else str(record.get("vintage") or "").strip() or None,
                "credit_status": str(record.get("credit_status") or "").strip() or None,
                "date_certified": date_certified.isoformat() if date_certified else None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_reserve_pool_registry",
                "freshness_state": "fresh",
                "freshness_basis": "public_registry_workbook_fetch_timestamp",
                "freshness_age_seconds": 0.0,
                "session_status": "reserve_pool_snapshot",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Maryland WQT reserve pool workbook",
                "source_url": source_url,
                "source_sheet": "MD_Reserve",
                "source_program_url": PROGRAM_URL,
                "candidate_reject_reason": "official_reserve_pool_registry_not_order_routable",
            }
        )

    trade_records = _sheet_records(
        sheets,
        "All_Trades",
        {
            "credit_ids",
            "generator",
            "owner",
            "watershed",
            "vintage",
            "credit_type",
            "credits",
            "new_owner",
            "date_registered",
        },
    )
    invalid_trades = 0
    for record in trade_records[: max(1, int(limit_per_sheet))]:
        pollutant_name, pollutant_code, base_asset = _pollutant(record.get("credit_type"))
        traded_credits = number(record.get("credits"))
        credit_ids = str(record.get("credit_ids") or "").strip()
        generator = str(record.get("generator") or "").strip()
        if traded_credits is None or not credit_ids or not generator:
            invalid_trades += 1
            continue
        registered_date = _excel_date(record.get("date_registered"))
        reported_acquired = number(record.get("credits_acquired"))
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:TRADE:{_stable_id(credit_ids, generator, record.get('new_owner'), pollutant_code)}",
                "instrument_id": f"{VENUE}:TRADE:{_stable_id(credit_ids, generator, record.get('new_owner'), pollutant_code)}",
                "symbol": f"MD_WQT_{pollutant_code}_TRADE",
                "name": f"Maryland registered {pollutant_name} credit trade",
                "base": base_asset,
                "quote": "LB_REDUCTION",
                "market_type": "registered_trade_reference",
                "market_surface": TRADES_SURFACE,
                "asset_class": "water_quality_trading_credit",
                "trade_type": "official_registered_trade_reference",
                "direction": "watch_only",
                "last": traded_credits,
                "price_available": False,
                "pollutant": pollutant_name,
                "traded_credits": traded_credits,
                "credit_ids": credit_ids,
                "generator": generator,
                "previous_owner": str(record.get("owner") or "").strip() or None,
                "new_owner": str(record.get("new_owner") or "").strip() or None,
                "watershed": str(record.get("watershed") or "").strip() or None,
                "vintage_year": int(str(record.get("vintage") or "").strip())
                if str(record.get("vintage") or "").strip().isdigit()
                else str(record.get("vintage") or "").strip() or None,
                "registered_date": registered_date.isoformat() if registered_date else None,
                "applied_to_permit": str(record.get("applied_to_permit") or "").strip() or None,
                "permit_number": str(record.get("permit") or "").strip() or None,
                "reported_credits_acquired_value": (
                    None
                    if registered_date and reported_acquired == float((registered_date - dt.date(1899, 12, 30)).days)
                    else reported_acquired
                ),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_registered_trade_reference",
                "freshness_state": "fresh",
                "freshness_basis": "public_registry_workbook_fetch_timestamp",
                "freshness_age_seconds": 0.0,
                "session_status": "trade_registered",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Maryland WQT registered trades workbook",
                "source_url": source_url,
                "source_sheet": "All_Trades",
                "source_program_url": PROGRAM_URL,
                "candidate_reject_reason": "official_registered_trade_reference_not_executable_quote",
            }
        )

    if not rows:
        details = []
        if invalid_generated:
            details.append(f"{invalid_generated} generated rows invalid")
        if invalid_reserve:
            details.append(f"{invalid_reserve} reserve rows invalid")
        if invalid_trades:
            details.append(f"{invalid_trades} trade rows invalid")
        suffix = f"; {'; '.join(details)}" if details else ""
        raise MarylandWaterQualityTradingParseError(f"no usable registry workbook rows{suffix}")
    return rows


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
    source_url: str,
    result: dict[str, Any],
    surface: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, surface)
    row.update(
        {
            "inst_id": f"{VENUE}:ADAPTER_HEALTH:{slug(surface)}",
            "instrument_id": f"{VENUE}:ADAPTER_HEALTH:{slug(surface)}",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_program_url": PROGRAM_URL,
            "candidate_reject_reason": (
                "public_maryland_wqt_parser_failure"
                if parser_error
                else "public_maryland_wqt_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class MarylandDepartmentOfTheEnvironmentAdapter:
    info = AdapterInfo(
        adapter_id="maryland_department_of_the_environment",
        venue=VENUE,
        market_type="water_quality_trading_credit_registry",
        source="Maryland Department of the Environment Water Quality Trading public registry and market board",
        capabilities=(
            "public_market_data",
            "nutrient_credit_registry",
            "market_board_listing",
            "event_price_reference",
            "nitrogen_reduction_credit",
            "phosphorus_reduction_credit",
            "sediment_reduction_credit",
            "registered_trade_reference",
            "reserve_pool_reference",
            "source_health",
        ),
        aliases=(
            "maryland department of the environment",
            "maryland water quality trading",
            "maryland wqt",
            "wqt registry and marketplace",
            "maryland nutrient sediment credits",
            "maryland market board",
        ),
        docs_url=PROGRAM_URL,
        runtime_entrypoint=(
            "adapters.venues.maryland_department_of_the_environment."
            "MarylandDepartmentOfTheEnvironmentAdapter"
        ),
        quote_assets=("USD_PER_LB_REDUCTION", "LB_REDUCTION"),
        default_cache_minutes=120,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 20)))
        board_limit = max(1, min(int(cfg.get("max_market_board_rows", 500)), 5_000))
        workbook_limit = max(1, min(int(cfg.get("max_registry_rows_per_sheet", 10_000)), 50_000))
        max_workbook_bytes = max(250_000, min(int(cfg.get("max_workbook_bytes", 5_000_000)), 25_000_000))
        sources = (
            (
                "pricing_page",
                str(cfg.get("program_url") or PROGRAM_URL),
                PRICING_SURFACE,
                fetch_text,
                parse_maryland_wqt_market_pricing,
                {"source_url": str(cfg.get("program_url") or PROGRAM_URL)},
            ),
            (
                "market_board",
                str(cfg.get("market_board_url") or MARKET_BOARD_URL),
                MARKET_BOARD_SURFACE,
                fetch_text,
                parse_maryland_wqt_market_board,
                {
                    "source_url": str(cfg.get("market_board_url") or MARKET_BOARD_URL),
                    "limit": board_limit,
                },
            ),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        for source_name, source_url, surface, fetcher, parser, parser_kwargs in sources:
            result = fetcher(source_url, timeout)
            fetch_status[source_name] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                observations.extend(
                    parser(
                        str(result.get("text") or ""),
                        received_at=result.get("received_at"),
                        **parser_kwargs,
                    )
                )
                usable_sources += 1
            except (MarylandWaterQualityTradingParseError, TypeError, ValueError) as exc:
                message = f"Maryland WQT {source_name} parser failed: {exc}"[:300]
                parser_failures.append({"source": source_name, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_url, result, surface, message))

        workbook_url = str(cfg.get("registry_workbook_url") or REGISTRY_WORKBOOK_URL)
        workbook_result = fetch_bytes(workbook_url, timeout, max_bytes=max_workbook_bytes)
        fetch_status["registry_workbook"] = _fetch_evidence(workbook_result, workbook_url)
        if not workbook_result.get("ok"):
            observations.append(_failure_observation(workbook_url, workbook_result, GENERATED_SURFACE))
        else:
            try:
                observations.extend(
                    parse_maryland_wqt_registry_workbook(
                        workbook_result.get("content") or b"",
                        source_url=workbook_url,
                        received_at=workbook_result.get("received_at"),
                        limit_per_sheet=workbook_limit,
                    )
                )
                usable_sources += 1
            except (
                MarylandWaterQualityTradingParseError,
                ET.ParseError,
                TypeError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                message = f"Maryland WQT registry workbook parser failed: {exc}"[:300]
                parser_failures.append({"source": "registry_workbook", "source_url": workbook_url, "error": message})
                observations.append(_failure_observation(workbook_url, workbook_result, GENERATED_SURFACE, message))

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        source_status = (
            "reachable"
            if usable_sources == 3 and not parser_failures
            else "degraded"
            if usable_sources or parser_failures
            else "blocked"
            if "blocked" in statuses
            else "unavailable"
        )
        real_observations = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1255,
                "source_status": source_status,
                "source_url": PROGRAM_URL,
                "source_urls": [PROGRAM_URL, MARKET_BOARD_URL, REGISTRY_WORKBOOK_URL, PURCHASING_FAQ_URL],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0]
                if len(freshness_states) == 1
                else "mixed"
                if freshness_states
                else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0]
                if len(session_states) == 1
                else "mixed"
                if session_states
                else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "pricing_observation_count": sum(1 for row in real_observations if row.get("market_surface") == PRICING_SURFACE),
                "market_board_observation_count": sum(
                    1 for row in real_observations if row.get("market_surface") == MARKET_BOARD_SURFACE
                ),
                "registry_observation_count": sum(
                    1
                    for row in real_observations
                    if row.get("market_surface") in {GENERATED_SURFACE, RESERVE_SURFACE, TRADES_SURFACE}
                ),
                "capability_gap": (
                    "public_market_board_exposes_quantities_and_summary_price_ranges_but_not_live_anonymous_orderable_quotes"
                ),
                "paper_only": True,
            },
        )


register_adapter(MarylandDepartmentOfTheEnvironmentAdapter())
