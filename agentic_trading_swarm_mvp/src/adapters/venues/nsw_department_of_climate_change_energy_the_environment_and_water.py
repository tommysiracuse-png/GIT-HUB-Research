"""NSW Biodiversity Offsets Scheme public-register adapter.

The NSW Department publishes point-in-time, public exports for biodiversity
credit supply and transactions.  They are statutory market-information
registers, not executable venues: this adapter keeps every observation
watch-only and never exposes contact fields carried by the source exports.
"""

from __future__ import annotations

import datetime as dt
import html
import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, health_observation, html_tables, number, slug, utc_now
from scan_batch import ScanBatch


PUBLIC_REGISTERS_URL = (
    "https://www.environment.nsw.gov.au/topics/animals-and-plants/"
    "biodiversity-offsets-scheme/maps-systems-and-resources/public-registers"
)
CREDIT_SUPPLY_PAGE_URL = (
    "https://www.environment.nsw.gov.au/topics/animals-and-plants/"
    "biodiversity-offsets-scheme/biodiversity-credits-market/find-credit-buyers-and-sellers/"
    "credit-supply-register"
)
SUMMARY_TABLES_URL = (
    "https://www.environment.nsw.gov.au/topics/animals-and-plants/"
    "biodiversity-offsets-scheme/biodiversity-credits-market/summary-tables"
)
SUPPLY_EXPORT_URL = "https://customer.lmbc.nsw.gov.au/application/BOAMCreditSupplyRegisterExport"
TRANSACTIONS_EXPORT_URL = "https://customer.lmbc.nsw.gov.au/application/BOAMCreditTransactionSaleRegisterExport"

VENUE = "NSW_DCCEEW"
SUPPLY_SURFACE = "nsw_biodiversity_offsets_scheme_credit_supply"
TRANSACTIONS_SURFACE = "nsw_biodiversity_offsets_scheme_credit_transactions"


class NswBiodiversityRegisterParseError(ValueError):
    """Raised when a reachable NSW public-register export changes schema."""


def _local_name(value: str) -> str:
    return str(value).rsplit("}", 1)[-1]


def _field_token(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def _cell_text(value: Any) -> str:
    text = str(value or "").strip()
    # Excel's HTML export represents identifier-like values as ="000123".
    if len(text) >= 3 and text.startswith('="') and text.endswith('"'):
        return text[2:-1]
    return text


def _spreadsheetml_tables(content: bytes) -> list[list[list[str]]]:
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise NswBiodiversityRegisterParseError("unsupported XML document declaration")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise NswBiodiversityRegisterParseError(f"invalid SpreadsheetML export: {exc}") from exc
    tables: list[list[list[str]]] = []
    for table in (node for node in root.iter() if _local_name(node.tag) == "Table"):
        rows: list[list[str]] = []
        for row in (node for node in table if _local_name(node.tag) == "Row"):
            values: list[str] = []
            for cell in (node for node in row if _local_name(node.tag) == "Cell"):
                index = next(
                    (value for key, value in cell.attrib.items() if _local_name(key) == "Index"),
                    None,
                )
                if index:
                    try:
                        target = max(0, int(index) - 1)
                    except ValueError:
                        target = len(values)
                    while len(values) < target:
                        values.append("")
                values.append(" ".join("".join(cell.itertext()).split()))
            if any(values):
                rows.append(values)
        if rows:
            tables.append(rows)
    return tables


def _tolerant_spreadsheetml_tables(content: bytes) -> list[list[list[str]]]:
    """Read SpreadsheetML cells when the public export is not valid XML.

    The source has emitted unescaped display text in otherwise conventional
    SpreadsheetML.  This deliberately narrow fallback accepts only its Table /
    Row / Cell structure and does not execute or interpret workbook content.
    """

    text = content.decode("utf-8", errors="replace")
    tables: list[list[list[str]]] = []
    table_pattern = re.compile(r"<(?:[\w.-]+:)?Table\b[^>]*>(.*?)</(?:[\w.-]+:)?Table\s*>", re.IGNORECASE | re.DOTALL)
    row_pattern = re.compile(r"<(?:[\w.-]+:)?Row\b[^>]*>(.*?)</(?:[\w.-]+:)?Row\s*>", re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(
        r"<(?:[\w.-]+:)?Cell\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[\w.-]+:)?Cell\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    index_pattern = re.compile(r"(?:[\w.-]+:)?Index\s*=\s*[\"'](\d+)[\"']", re.IGNORECASE)
    for table_match in table_pattern.finditer(text):
        table: list[list[str]] = []
        for row_match in row_pattern.finditer(table_match.group(1)):
            values: list[str] = []
            for cell_match in cell_pattern.finditer(row_match.group(1)):
                index = index_pattern.search(cell_match.group("attrs"))
                if index:
                    while len(values) < max(0, int(index.group(1)) - 1):
                        values.append("")
                value = re.sub(r"<[^>]+>", "", cell_match.group("body"))
                values.append(" ".join(html.unescape(value).split()))
            if any(values):
                table.append(values)
        if table:
            tables.append(table)
    return tables


def _workbook_tables(content: bytes) -> list[list[list[str]]]:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise NswBiodiversityRegisterParseError("public-register export is empty")
    body = bytes(content)
    prefix = body.lstrip()[:200].lower()
    if prefix.startswith(b"<?xml") or b"<workbook" in prefix:
        try:
            tables = _spreadsheetml_tables(body)
        except NswBiodiversityRegisterParseError as exc:
            # The live export is SpreadsheetML-shaped but has historically
            # contained unescaped display text. HTMLParser is deliberately
            # tolerant of that public-export defect and still preserves its
            # table boundaries and cell text.
            tables = _tolerant_spreadsheetml_tables(body)
            if not tables:
                raise exc
    else:
        tables = html_tables(body.decode("utf-8", errors="replace"))
    if not tables:
        raise NswBiodiversityRegisterParseError("public-register export contained no tables")
    return tables


def _table_records(content: bytes, required: set[str]) -> list[dict[str, str]]:
    for table in _workbook_tables(content):
        for position, header_row in enumerate(table):
            headers = [_field_token(value) for value in header_row]
            if not required.issubset(set(headers)):
                continue
            records: list[dict[str, str]] = []
            for raw_row in table[position + 1 :]:
                if not any(str(value or "").strip() for value in raw_row):
                    continue
                record = {
                    header: _cell_text(raw_row[index]) if index < len(raw_row) else ""
                    for index, header in enumerate(headers)
                    if header
                }
                records.append(record)
            return records
    raise NswBiodiversityRegisterParseError(
        "public-register header missing required fields: " + ", ".join(sorted(required))
    )


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NswBiodiversityRegisterParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date(value: Any) -> dt.date | None:
    text = _cell_text(value)
    if not text:
        return None
    for pattern in ("%B %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _freshness(event_date: dt.date, received_at: dt.datetime, stale_after_days: float) -> tuple[str, float]:
    age = max(0.0, (received_at - dt.datetime.combine(event_date, dt.time(), tzinfo=dt.timezone.utc)).total_seconds())
    return (
        "fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale",
        round(age, 3),
    )


def _stable_id(*values: Any) -> str:
    digest = hashlib.sha256("|".join(_cell_text(value) for value in values).encode("utf-8")).hexdigest()
    return digest[:16].upper()


def parse_nsw_biodiversity_credit_supply(
    content: bytes,
    *,
    source_url: str = SUPPLY_EXPORT_URL,
    received_at: str | None = None,
    limit: int = 100_000,
) -> list[dict[str, Any]]:
    """Normalize issued, pending, and equivalent credit supply without contacts."""

    records = _table_records(content, {"credit_id", "credit_status", "number_of_credits"})
    fetched_at = _received_time(received_at)
    parsed: list[dict[str, Any]] = []
    invalid_rows = 0
    for record in records:
        credit_id = _cell_text(record.get("credit_id"))
        status = _cell_text(record.get("credit_status"))
        quantity = number(record.get("number_of_credits"))
        if not credit_id or not status or quantity is None or quantity < 0:
            invalid_rows += 1
            continue
        issued_date = _date(record.get("date_credits_issued"))
        credit_kind = _cell_text(record.get("ecosystem_or_species")) or "unspecified"
        species = _cell_text(record.get("species_scientific_name"))
        pct = _cell_text(record.get("plant_community_type_common_name"))
        identity = _stable_id(credit_id, credit_kind, species, pct, record.get("ibra_subregion"))
        parsed.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:BOS_SUPPLY:{identity}",
                "instrument_id": f"{VENUE}:BOS_SUPPLY:{identity}",
                "symbol": "NSW_BIODIVERSITY_CREDIT_SUPPLY",
                "name": f"NSW biodiversity credit supply {credit_id}",
                "base": "NSW_BIODIVERSITY_CREDIT",
                "quote": "AUD_PER_CREDIT",
                "market_type": "credit_supply_registry",
                "market_surface": SUPPLY_SURFACE,
                "asset_class": "biodiversity_credit",
                "trade_type": "official_credit_supply_reference",
                "direction": "watch_only",
                "last": quantity,
                "credit_id": credit_id,
                "credit_status": status,
                "credit_quantity": quantity,
                "credit_kind": credit_kind,
                "ibra_subregion": _cell_text(record.get("ibra_subregion")) or None,
                "ibra_region": _cell_text(record.get("ibra_region")) or None,
                "offset_trading_group": _cell_text(record.get("offset_trading_group")) or None,
                "vegetation_formation": _cell_text(record.get("vegetation_formation")) or None,
                "species_scientific_name": species or None,
                "species_common_name": _cell_text(record.get("species_common_name")) or None,
                "plant_community_type": pct or None,
                "issued_date": issued_date.isoformat() if issued_date else None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_credit_supply_register",
                "freshness_state": "fresh",
                "freshness_basis": "point_in_time_public_register_export",
                "freshness_age_seconds": 0.0,
                "session_status": "public_register_snapshot",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "NSW Biodiversity Offsets Scheme Credit Supply Register",
                "source_url": source_url,
                "source_public_register_url": PUBLIC_REGISTERS_URL,
                "source_supply_page_url": CREDIT_SUPPLY_PAGE_URL,
                "candidate_reject_reason": "public_credit_supply_register_not_order_routable",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise NswBiodiversityRegisterParseError(f"no usable credit-supply rows{detail}")
    return parsed[: max(1, int(limit))]


def parse_nsw_biodiversity_credit_transactions(
    content: bytes,
    *,
    source_url: str = TRANSACTIONS_EXPORT_URL,
    received_at: str | None = None,
    stale_after_days: float = 365.0,
    limit: int = 100_000,
) -> list[dict[str, Any]]:
    """Normalize public credit transactions, including realised sale prices."""

    required = {
        "transaction_date",
        "transaction_id",
        "transaction_status",
        "transaction_type",
        "number_of_credits",
        "price_per_credit_ex_gst",
    }
    records = _table_records(content, required)
    fetched_at = _received_time(received_at)
    parsed: list[dict[str, Any]] = []
    invalid_rows = 0
    for record in records:
        transaction_id = _cell_text(record.get("transaction_id"))
        transaction_date = _date(record.get("transaction_date"))
        quantity = number(record.get("number_of_credits"))
        price = number(record.get("price_per_credit_ex_gst"))
        if not transaction_id or transaction_date is None or quantity is None or quantity < 0 or price is None or price < 0:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(transaction_date, fetched_at, stale_after_days)
        credit_kind = _cell_text(record.get("plant_community_type")) or _cell_text(record.get("scientific_name")) or "unspecified"
        transaction_type = _cell_text(record.get("transaction_type"))
        parsed.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:BOS_TRANSACTION:{transaction_id}",
                "instrument_id": f"{VENUE}:BOS_TRANSACTION:{transaction_id}",
                "symbol": "NSW_BIODIVERSITY_CREDIT_TRANSACTION",
                "name": f"NSW biodiversity credit transaction {transaction_id}",
                "base": "NSW_BIODIVERSITY_CREDIT",
                "quote": "AUD_PER_CREDIT",
                "market_type": "credit_transaction_registry",
                "market_surface": TRANSACTIONS_SURFACE,
                "asset_class": "biodiversity_credit",
                "trade_type": "official_credit_transaction_reference",
                "direction": "watch_only",
                "last": price,
                "transaction_id": transaction_id,
                "transaction_date": transaction_date.isoformat(),
                "transaction_status": _cell_text(record.get("transaction_status")),
                "transaction_type": transaction_type,
                "credit_quantity": quantity,
                "sale_price_per_credit_aud_ex_gst": price,
                "transaction_value_aud_ex_gst": round(quantity * price, 6),
                "credit_kind": credit_kind,
                "ibra_subregion": _cell_text(record.get("sub_region")) or None,
                "offset_trading_group": _cell_text(record.get("offset_trading_group")) or None,
                "vegetation_formation": _cell_text(record.get("vegetation_formation")) or None,
                "species_scientific_name": _cell_text(record.get("scientific_name")) or None,
                "species_common_name": _cell_text(record.get("common_name")) or None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_credit_transaction_sale_register",
                "freshness_state": freshness_state,
                "freshness_basis": "official_transaction_date",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed" if transaction_type.lower() in {"transfer", "retire"} else "reported",
                "observed_at": dt.datetime.combine(transaction_date, dt.time(), tzinfo=dt.timezone.utc).isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "NSW Biodiversity Offsets Scheme Credit Transactions Register",
                "source_url": source_url,
                "source_public_register_url": PUBLIC_REGISTERS_URL,
                "source_summary_tables_url": SUMMARY_TABLES_URL,
                "candidate_reject_reason": "historical_credit_transaction_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise NswBiodiversityRegisterParseError(f"no usable credit-transaction rows{detail}")
    parsed.sort(key=lambda row: str(row["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


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
    source_url: str, result: dict[str, Any], surface: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
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
            "source_public_register_url": PUBLIC_REGISTERS_URL,
            "candidate_reject_reason": (
                "public_biodiversity_register_parser_failure"
                if parser_error
                else "public_biodiversity_register_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class NswDepartmentOfClimateChangeEnergyTheEnvironmentAndWaterAdapter:
    info = AdapterInfo(
        adapter_id="nsw_department_of_climate_change_energy_the_environment_and_water",
        venue=VENUE,
        market_type="biodiversity_credit_public_register",
        source="NSW DCCEEW public Biodiversity Offsets Scheme credit registers",
        capabilities=(
            "public_market_data",
            "event_price_reference",
            "issued_credits",
            "pending_credits",
            "equivalent_biobanking_credits",
            "credit_supply",
            "credit_transaction_sale_price",
            "source_health",
        ),
        aliases=(
            "nsw department of climate change energy the environment and water",
            "new south wales biodiversity offsets scheme",
            "nsw biodiversity credits",
            "biodiversity offsets scheme credit supply register",
            "biodiversity credit transactions register",
            "biobanking credits",
        ),
        docs_url=PUBLIC_REGISTERS_URL,
        runtime_entrypoint=(
            "adapters.venues.nsw_department_of_climate_change_energy_the_environment_and_water."
            "NswDepartmentOfClimateChangeEnergyTheEnvironmentAndWaterAdapter"
        ),
        quote_assets=("AUD_PER_CREDIT",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 30)))
        max_supply_bytes = max(1_000_000, min(int(cfg.get("max_supply_bytes", 20_000_000)), 60_000_000))
        max_transaction_bytes = max(1_000_000, min(int(cfg.get("max_transaction_bytes", 16_000_000)), 60_000_000))
        max_rows = max(1, min(int(cfg.get("max_rows_per_register", 100_000)), 200_000))
        sources = (
            ("credit_supply", SUPPLY_EXPORT_URL, SUPPLY_SURFACE, parse_nsw_biodiversity_credit_supply, {"limit": max_rows}, max_supply_bytes),
            (
                "credit_transactions",
                TRANSACTIONS_EXPORT_URL,
                TRANSACTIONS_SURFACE,
                parse_nsw_biodiversity_credit_transactions,
                {"limit": max_rows, "stale_after_days": max(0.0, float(cfg.get("transaction_stale_after_days", 365.0)))},
                max_transaction_bytes,
            ),
        )
        observations: list[dict] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0
        for source_name, source_url, surface, parser, parser_options, max_bytes in sources:
            result = fetch_bytes(source_url, timeout, max_bytes=max_bytes)
            fetch_status[source_name] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                rows = parser(
                    result.get("content") or b"",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    **parser_options,
                )
            except (NswBiodiversityRegisterParseError, ET.ParseError, TypeError, ValueError) as exc:
                message = f"NSW DCCEEW {source_name} parser failed: {exc}"[:300]
                parser_failures.append({"source": source_name, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_url, result, surface, message))
                continue
            observations.extend(rows)
            usable_sources += 1

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        source_status = (
            "reachable"
            if usable_sources == len(sources) and not parser_failures
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
                "adapter_spec_id": 1539,
                "source_status": source_status,
                "source_url": PUBLIC_REGISTERS_URL,
                "source_urls": [PUBLIC_REGISTERS_URL, CREDIT_SUPPLY_PAGE_URL, SUMMARY_TABLES_URL, SUPPLY_EXPORT_URL, TRANSACTIONS_EXPORT_URL],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_observations),
                "capability_gap": "public_register_data_is_not_an_executable_quote_or_order_route",
                "paper_only": True,
            },
        )


register_adapter(NswDepartmentOfClimateChangeEnergyTheEnvironmentAndWaterAdapter())
