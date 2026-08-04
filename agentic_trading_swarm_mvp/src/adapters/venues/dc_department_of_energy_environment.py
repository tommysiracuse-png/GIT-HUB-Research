"""Public DC stormwater-retention-credit registry adapter.

The District's Quickbase reports are public bilateral-market references.  The
adapter requests only the non-contact columns needed for market research and
keeps every observation watch-only; neither report is an executable venue.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, slug, utc_now
from scan_batch import ScanBatch


PROGRAM_RESOURCES_URL = "https://doee.dc.gov/service/src-program-resources"
PURCHASING_URL = "https://doee.dc.gov/service/purchasing-and-using-stormwater-retention-credits"
REGISTRY_URL = "https://octo.quickbase.com/db/bjt7u8rgq?a=dbpage&pagename=shell.html"

FINAL_SALES_BASE_URL = "https://octo.quickbase.com/db/biqmxeww9"
FOR_SALE_BASE_URL = "https://octo.quickbase.com/db/bpwqxvsbf"
FINAL_SALES_COLUMNS = "15.51.55.21.16.32.90.89.19.26"
FOR_SALE_COLUMNS = "10.21.100.16"


def _final_sales_url(limit: int) -> str:
    return (
        f"{FINAL_SALES_BASE_URL}?a=API_DoQuery&qid=37&clist={FINAL_SALES_COLUMNS}"
        f"&fmt=structured&slist=15&options=sortorder-D.num-{max(1, int(limit))}"
    )


def _for_sale_url(limit: int) -> str:
    return (
        f"{FOR_SALE_BASE_URL}?a=API_DoQuery&qid=8&clist={FOR_SALE_COLUMNS}"
        f"&fmt=structured&options=num-{max(1, int(limit))}"
    )


FINAL_SALES_URL = _final_sales_url(250)
FOR_SALE_URL = _for_sale_url(100)


class DcSrcRegistryParseError(ValueError):
    """Raised when a reachable public registry report changes schema."""


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _registry_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("\u00a0", " ")
    if "," in text:
        compact = text.replace(" ", "")
        if "." in compact or re.search(r",\d{3}(?:,\d{3})*(?:\D|$)", compact):
            text = text.replace(",", "")
    return number(text)


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _structured_rows(text: str, allowed_labels: set[str]) -> list[tuple[str, dict[str, str]]]:
    if not isinstance(text, str) or not text.strip():
        raise DcSrcRegistryParseError("empty Quickbase XML response")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise DcSrcRegistryParseError("unsupported XML document declaration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DcSrcRegistryParseError(f"invalid Quickbase XML: {exc}") from exc

    errcode = _text(root.find("errcode"))
    if errcode not in {"", "0"}:
        detail = _text(root.find("errtext")) or _text(root.find("errdetail")) or "unknown error"
        raise DcSrcRegistryParseError(f"Quickbase report error {errcode}: {detail}")
    table = root.find("table")
    if table is None:
        raise DcSrcRegistryParseError("Quickbase table element was not found")

    labels: dict[str, str] = {}
    for field in table.findall("./fields/field"):
        field_id = str(field.get("id") or _text(field.find("id")))
        label = _token(_text(field.find("label")))
        if field_id and label in allowed_labels:
            labels[field_id] = label
    missing = sorted(allowed_labels - set(labels.values()))
    if missing:
        raise DcSrcRegistryParseError(f"required report fields were not found: {', '.join(missing)}")

    records: list[tuple[str, dict[str, str]]] = []
    for index, record in enumerate(table.findall("./records/record"), start=1):
        row: dict[str, str] = {}
        for cell in record.findall("f"):
            field_id = str(cell.get("id") or "")
            if field_id in labels:
                row[labels[field_id]] = _text(cell)
        record_id = str(record.get("rid") or row.get("record_id") or index)
        records.append((record_id, row))
    return records


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _registry_date(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        seconds = numeric / 1000.0 if abs(numeric) >= 100_000_000_000 else numeric
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = dt.datetime.strptime(raw, pattern)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _freshness(
    event_time: dt.datetime, received_at: str | None, stale_after_days: float
) -> tuple[str, float]:
    age = max(0.0, (_received_time(received_at) - event_time).total_seconds())
    state = "fresh" if age <= max(0.0, stale_after_days) * 86400.0 else "stale"
    return state, round(age, 3)


def _watersheds(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;/]", value) if part.strip()]


def _stable_id(record_id: str, values: list[Any]) -> str:
    if record_id and record_id.isdigit():
        return record_id
    digest = hashlib.sha256("|".join(str(value or "") for value in values).encode("utf-8")).hexdigest()
    return digest[:12].upper()


def parse_dc_src_final_sales(
    text: str,
    *,
    source_url: str = FINAL_SALES_URL,
    received_at: str | None = None,
    stale_after_days: float = 365.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize the official Final SRC Sale Prices structured XML report."""

    required = {
        "transfer_date",
        "watershed_where_srcs_are_generated",
        "sewershed_where_srcs_are_generated",
        "number_of_srcs",
        "purchase_price_per_src",
        "value_of_transfer_paid_by_buyer",
        "transferred_srcs_bmp_installation_date",
        "transferred_srcs_type_of_activity",
        "start_serial_number",
        "end_serial_number",
    }
    records = _structured_rows(text, required)
    parsed: list[dict] = []
    invalid_rows = 0
    for record_id, row in records:
        event_time = _registry_date(row.get("transfer_date"))
        quantity = _registry_number(row.get("number_of_srcs"))
        price = _registry_number(row.get("purchase_price_per_src"))
        watershed = str(row.get("watershed_where_srcs_are_generated") or "").strip()
        if event_time is None or quantity is None or quantity <= 0 or price is None or price <= 0 or not watershed:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_days)
        sale_id = _stable_id(
            record_id,
            [event_time.date(), watershed, row.get("start_serial_number"), quantity, price],
        )
        install_time = _registry_date(row.get("transferred_srcs_bmp_installation_date"))
        parsed.append(
            {
                "venue": "DC_DOEE",
                "inst_id": f"DC_DOEE:SRC_SALE:{sale_id}",
                "instrument_id": f"DC_DOEE:SRC_SALE:{sale_id}",
                "symbol": f"SRC_SALE_{sale_id}",
                "name": f"District of Columbia SRC sale {sale_id}",
                "base": "DC_SRC",
                "quote": "USD_PER_SRC",
                "market_type": "sale_reference",
                "market_surface": "dc_stormwater_retention_credit_sales",
                "asset_class": "stormwater_retention_credit",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "sale_price_per_src": price,
                "quantity_src": quantity,
                "transfer_value_usd": _registry_number(row.get("value_of_transfer_paid_by_buyer")),
                "watershed": watershed,
                "watersheds": _watersheds(watershed),
                "sewershed": str(row.get("sewershed_where_srcs_are_generated") or "").strip() or None,
                "activity_type": str(row.get("transferred_srcs_type_of_activity") or "").strip() or None,
                "bmp_installation_date": install_time.date().isoformat() if install_time else None,
                "start_serial_number": str(row.get("start_serial_number") or "").strip() or None,
                "end_serial_number": str(row.get("end_serial_number") or "").strip() or None,
                "transfer_date": event_time.date().isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_registry_sale_record",
                "freshness_state": freshness_state,
                "freshness_basis": "official_transfer_date",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "DC DOEE Final SRC Sale Prices registry",
                "source_url": source_url,
                "source_registry_url": REGISTRY_URL,
                "source_notice_url": PROGRAM_RESOURCES_URL,
                "candidate_reject_reason": "historical_bilateral_sale_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} report rows were invalid" if invalid_rows else ""
        raise DcSrcRegistryParseError(f"no usable final SRC sale rows{detail}")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def parse_dc_src_for_sale(
    text: str,
    *,
    source_url: str = FOR_SALE_URL,
    received_at: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Normalize safe market columns from the official SRCs for Sale report."""

    required = {"number_of_srcs_for_sale", "buyer_s_price", "src_type", "src_watershed"}
    records = _structured_rows(text, required)
    parsed: list[dict] = []
    invalid_rows = 0
    for record_id, row in records:
        quantity = _registry_number(row.get("number_of_srcs_for_sale"))
        price = _registry_number(row.get("buyer_s_price"))
        watershed = str(row.get("src_watershed") or "").strip()
        src_type = str(row.get("src_type") or "").strip()
        if quantity is None or quantity <= 0 or price is None or price <= 0 or not watershed:
            invalid_rows += 1
            continue
        listing_id = _stable_id(record_id, [watershed, src_type, quantity, price])
        observed_at = received_at or utc_now()
        parsed.append(
            {
                "venue": "DC_DOEE",
                "inst_id": f"DC_DOEE:SRC_FOR_SALE:{listing_id}",
                "instrument_id": f"DC_DOEE:SRC_FOR_SALE:{listing_id}",
                "symbol": f"SRC_FOR_SALE_{listing_id}",
                "name": f"District of Columbia SRC listing {listing_id}",
                "base": "DC_SRC",
                "quote": "USD_PER_SRC",
                "market_type": "seller_listing",
                "market_surface": "dc_stormwater_retention_credits_for_sale",
                "asset_class": "stormwater_retention_credit",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "buyer_price_per_src": price,
                "price_kind": "registry_buyer_price",
                "source_price_field": "Buyer's price",
                "quantity_src_for_sale": quantity,
                "src_type": src_type or None,
                "watershed": watershed,
                "watersheds": _watersheds(watershed),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_registry_seller_listing",
                "freshness_state": "fresh",
                "freshness_basis": "public_registry_fetch_timestamp",
                "freshness_age_seconds": 0.0,
                "session_status": "seller_listing_active",
                "observed_at": observed_at,
                "fetched_at": observed_at,
                "price_source": "DC DOEE SRCs for Sale registry",
                "source_url": source_url,
                "source_registry_url": REGISTRY_URL,
                "source_notice_url": PROGRAM_RESOURCES_URL,
                "candidate_reject_reason": "public_bilateral_listing_not_order_routable",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} report rows were invalid" if invalid_rows else ""
        raise DcSrcRegistryParseError(f"no usable SRC-for-sale rows{detail}")
    return parsed[: max(1, int(limit))]


# Descriptive compatibility names for callers that refer to the report by role.
parse_dc_src_sales = parse_dc_src_final_sales
parse_dc_src_listings = parse_dc_src_for_sale


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
    observation = health_observation("DC_DOEE", source_url, evidence, surface)
    health_id = f"DC_DOEE:ADAPTER_HEALTH:{slug(surface)}"
    observation.update(
        {
            "inst_id": health_id,
            "instrument_id": health_id,
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_registry_url": REGISTRY_URL,
            "source_notice_url": PROGRAM_RESOURCES_URL,
            "candidate_reject_reason": "public_reference_parser_failure"
            if parser_error
            else "public_reference_source_unavailable",
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class DcDepartmentOfEnergyEnvironmentAdapter:
    info = AdapterInfo(
        adapter_id="dc_department_of_energy_environment",
        venue="DC_DOEE",
        market_type="stormwater_retention_credit_registry",
        source="DC DOEE public SRC and Offv registry",
        capabilities=(
            "public_market_data",
            "event_price_reference",
            "seller_listing",
            "buyer_price",
            "sale_price",
            "supply_volume",
            "watershed",
            "sewershed",
            "source_health",
        ),
        aliases=(
            "dc department of energy and environment",
            "district of columbia department of energy environment",
            "dc doee",
            "stormwater retention credits",
            "src and offv registry",
            "off-site retention volume",
        ),
        docs_url=PROGRAM_RESOURCES_URL,
        runtime_entrypoint=(
            "adapters.venues.dc_department_of_energy_environment."
            "DcDepartmentOfEnergyEnvironmentAdapter"
        ),
        quote_assets=("USD_PER_SRC",),
        default_cache_minutes=30,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        sales_limit = max(1, min(int(cfg.get("max_sales_rows", 250)), 1000))
        listing_limit = max(1, min(int(cfg.get("max_listing_rows", 100)), 500))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 365.0)))
        sales_url = _final_sales_url(sales_limit)
        listings_url = _for_sale_url(listing_limit)
        sources = (
            (
                "final_sales",
                sales_url,
                "dc_stormwater_retention_credit_sales",
                parse_dc_src_final_sales,
                {"stale_after_days": stale_after_days, "limit": sales_limit},
            ),
            (
                "for_sale",
                listings_url,
                "dc_stormwater_retention_credits_for_sale",
                parse_dc_src_for_sale,
                {"limit": listing_limit},
            ),
        )
        observations: list[dict] = []
        parser_failures: list[dict[str, str]] = []
        source_health: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        for source_name, source_url, surface, parser, parser_options in sources:
            result = fetch_text(source_url, timeout)
            source_health[source_name] = _source_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                rows = parser(
                    result.get("text") or "",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    **parser_options,
                )
            except (DcSrcRegistryParseError, ET.ParseError, TypeError, ValueError) as exc:
                message = f"DC DOEE {source_name} parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source": source_name, "source_url": source_url, "error": message}
                )
                observations.append(_failure_observation(source_url, result, surface, message))
                continue
            observations.extend(rows)
            usable_sources += 1

        fetch_statuses = [item["fetch_status"] for item in source_health.values()]
        if usable_sources == len(sources) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in fetch_statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = sorted(
            {str(row.get("freshness_state") or "unknown") for row in observations}
        )
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        session_states = sorted(
            {str(row.get("session_status") or "unknown") for row in observations}
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1254,
                "source_status": source_status,
                "source_urls": [sales_url, listings_url, REGISTRY_URL, PROGRAM_RESOURCES_URL],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "sale_observation_count": sum(
                    1
                    for row in observations
                    if row.get("market_surface") == "dc_stormwater_retention_credit_sales"
                    and not row.get("parser_failure")
                ),
                "listing_observation_count": sum(
                    1
                    for row in observations
                    if row.get("market_surface") == "dc_stormwater_retention_credits_for_sale"
                    and not row.get("parser_failure")
                ),
                "paper_only": True,
                "contact_fields_requested": False,
            },
        )


register_adapter(DcDepartmentOfEnergyEnvironmentAdapter())
