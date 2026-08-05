"""Papua New Guinea Customs Service motor-vehicle TSC reference adapter.

PNG Customs publishes its motor-vehicle Tariff Specification Code (TSC) list
as a public notice.  The list is useful identity and classification evidence
for used-import research, but it is neither a duty quote nor an order venue.
Every normalized record is therefore a zero-price, watch-only reference.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, health_observation, utc_now
from scan_batch import ScanBatch


TSC_LIST_URL = "https://www.customs.gov.pg/pdf/info_sheet/TSCs%20List%20-Implementation.pdf"
TSC_LIST_MIRROR_URL = "https://www.customs.gov.pg/uploads/TSCs%20List%20-Implementation.pdf"
SOURCE_URL = TSC_LIST_URL
MARKET_SURFACE = "papua_new_guinea_motor_vehicle_tariff_specification_codes"
VENUE = "PNG_CUSTOMS"


class PapuaNewGuineaCustomsTscParseError(ValueError):
    """Raised when a reachable PNG Customs notice lacks usable TSC rows."""


def extract_pdf_text(body: bytes) -> str:
    """Extract visible text from PNG Customs' bounded public PDF notice."""

    if not isinstance(body, bytes) or not body:
        raise PapuaNewGuineaCustomsTscParseError("official TSC PDF response is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PapuaNewGuineaCustomsTscParseError(
            "pypdf is required to read the PNG Customs TSC PDF"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - upstream document drift is source evidence.
        raise PapuaNewGuineaCustomsTscParseError(f"official TSC PDF could not be read: {exc}") from exc
    if not text.strip():
        raise PapuaNewGuineaCustomsTscParseError("official TSC PDF contains no extractable text")
    return text


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PapuaNewGuineaCustomsTscParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _implementation_date(text: str) -> dt.date:
    match = re.search(
        r"(?:mandatory|implementation|effective|commenc(?:e|ing)).{0,100}?"
        r"(?:from|on|date\s*[:\-]?)?\s*(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(20\d{2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        # The notice itself identifies 1 June 2023 as the mandatory date.  A
        # strict fallback still rejects unrelated vehicle-TSC documents.
        match = re.search(
            r"\b(1)\s+(June)\s+(2023)\b", text, re.IGNORECASE
        )
    if not match:
        raise PapuaNewGuineaCustomsTscParseError("mandatory TSC implementation date was not found")
    try:
        return dt.datetime.strptime(" ".join(match.groups()), "%d %B %Y").date()
    except ValueError as exc:
        raise PapuaNewGuineaCustomsTscParseError("mandatory TSC implementation date is invalid") from exc


def _engine_capacity_cc(value: str) -> int | None:
    match = re.search(r"\b([0-9][0-9, ]{2,5})\s*(?:cc|c\.c\.|cm3)\b", value, re.IGNORECASE)
    if match:
        return int(re.sub(r"\D", "", match.group(1)))
    litres = re.search(r"\b([1-9](?:\.\d+)?)\s*(?:l|litre|liter)\b", value, re.IGNORECASE)
    return int(float(litres.group(1)) * 1000) if litres else None


def _year_range(value: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"\b((?:19|20)\d{2})\s*(?:[-\u2010-\u2015/]|to)\s*"
        r"((?:19|20)\d{2}|onwards|present|current)\b",
        value,
        re.IGNORECASE,
    )
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2).isdigit() else None
        return start, end
    years = re.findall(r"\b(?:19|20)\d{2}\b", value)
    return (int(years[0]), int(years[1]) if len(years) > 1 else None) if years else (None, None)


def _model_description(value: str, code_start: int) -> tuple[str | None, str | None, str | None]:
    prefix = value[:code_start]
    prefix = re.sub(r"\b(?:tariff\s+specification\s+code|tsc)\s*[:#-]?\s*$", "", prefix, flags=re.I)
    prefix = re.sub(r"^\s*(?:\d+\s*[.)-]\s*)?", "", prefix)
    make_match = re.search(
        r"\b(Toyota|Nissan|Mazda|Mitsubishi|Honda|Suzuki|Subaru|Isuzu|Ford|Hyundai|"
        r"Kia|Volkswagen|Mercedes(?:-Benz)?|BMW|Audi|Land Rover|Jeep|Lexus|Daihatsu)\b",
        prefix,
        re.IGNORECASE,
    )
    if not make_match:
        return None, None, None
    make = make_match.group(1).title().replace("Mercedes-Benz", "Mercedes-Benz")
    after_make = prefix[make_match.end() :]
    engine_match = re.search(r"\b([A-Z0-9]{2,}(?:-[A-Z0-9]+){1,3})\b", after_make, re.IGNORECASE)
    stop = engine_match.start() if engine_match else len(after_make)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", after_make)
    if year_match:
        stop = min(stop, year_match.start())
    capacity_match = re.search(r"\b[0-9][0-9, ]{2,5}\s*(?:cc|c\.c\.|cm3)\b|\b[1-9](?:\.\d+)?\s*(?:l|litre|liter)\b", after_make, re.I)
    if capacity_match:
        stop = min(stop, capacity_match.start())
    model = re.sub(r"^[\s|,;:/-]+|[\s|,;:/-]+$", "", after_make[:stop])
    return make, model or None, engine_match.group(1).upper() if engine_match else None


def _tsc_matches(text: str) -> list[re.Match[str]]:
    return list(
        re.finditer(
            r"\b(?:tariff\s+specification\s+code|TSC)\s*[:#-]?\s*"
            r"([A-Z0-9][A-Z0-9./_-]{1,})\b",
            text,
            re.IGNORECASE,
        )
    )


def parse_papua_new_guinea_customs_tscs(
    document: str | bytes,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize PNG Customs' public motor-vehicle TSC catalogue.

    PDF table extraction is inconsistent across viewers, so a record is built
    from the text immediately preceding each labelled TSC.  We require the
    manufacturer and model as well as the code; engine and year fields are
    retained whenever the notice provides them rather than inferred.
    """

    text = extract_pdf_text(document) if isinstance(document, bytes) else str(document or "")
    if not text.strip():
        raise PapuaNewGuineaCustomsTscParseError("official TSC document is empty")
    normalized = re.sub(r"[\u2010-\u2015]", "-", text)
    required_markers = ("tsc", "vehicle")
    if not all(marker in normalized.casefold() for marker in required_markers):
        raise PapuaNewGuineaCustomsTscParseError("document is missing TSC motor-vehicle markers")
    implementation_date = _implementation_date(normalized)
    matches = _tsc_matches(normalized)
    if not matches:
        raise PapuaNewGuineaCustomsTscParseError("document has no labelled tariff specification codes")

    fetched_at = _received_at(received_at)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, match in enumerate(matches):
        previous_end = matches[index - 1].end() if index else 0
        # A PDF extractor may join table cells into a single line.  Looking
        # back to the previous code still keeps each row's identifying fields.
        context = normalized[previous_end : match.start()][-600:]
        make, model, engine_code = _model_description(context, len(context))
        if not make or not model:
            continue
        code = match.group(1).upper()
        capacity_cc = _engine_capacity_cc(context)
        year_from, year_to = _year_range(context)
        key = (code, make, model.casefold())
        if key in seen:
            continue
        seen.add(key)
        suffix = re.sub(r"[^A-Z0-9]+", "_", f"{code}_{make}_{model}".upper()).strip("_")
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"PNG_CUSTOMS:TSC:{suffix}",
                "instrument_id": f"PNG_CUSTOMS:TSC:{suffix}",
                "symbol": f"TSC_{code}",
                "name": f"PNG Customs TSC {code}: {make} {model}",
                "base": "PNG_USED_MOTOR_VEHICLE",
                "quote": "N/A",
                "market_type": "vehicle_tariff_specification_catalog",
                "market_surface": MARKET_SURFACE,
                "asset_class": "used_motor_vehicle_tariff_classification",
                "trade_type": "official_customs_tariff_reference",
                "direction": "watch_only",
                "last": 0.0,
                "price_available": False,
                "tariff_specification_code": code,
                "vehicle_make": make,
                "vehicle_model": model,
                "engine_code": engine_code,
                "engine_capacity_cc": capacity_cc,
                "model_year_from": year_from,
                "model_year_to": year_to,
                "mandatory_from": implementation_date.isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_vehicle_tsc_catalog_record",
                "freshness_state": "fresh",
                "freshness_basis": "official_pdf_fetch",
                "freshness_age_seconds": 0.0,
                "session_status": "mandatory_in_force"
                if fetched_at.date() >= implementation_date
                else "scheduled",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Papua New Guinea Customs Service public TSC implementation notice",
                "source_url": source_url,
                "candidate_reject_reason": "official_tariff_specification_catalog_not_executable_quote",
            }
        )
    if not rows:
        raise PapuaNewGuineaCustomsTscParseError(
            "document has no usable vehicle make/model and tariff specification code rows"
        )
    return rows


# Short compatibility aliases for callers using the source's TSC terminology.
parse_png_customs_tscs = parse_papua_new_guinea_customs_tscs
parse_png_motor_vehicle_tscs = parse_papua_new_guinea_customs_tscs


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(
    result: dict[str, Any], source_url: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": "PNG_CUSTOMS:ADAPTER_HEALTH:TSC_LIST",
            "instrument_id": "PNG_CUSTOMS:ADAPTER_HEALTH:TSC_LIST",
            "symbol": "TSC_LIST_HEALTH",
            "base": "PNG_USED_MOTOR_VEHICLE",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_png_customs_tsc_parser_failure"
                if parser_error
                else "public_png_customs_tsc_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class PapuaNewGuineaCustomsServiceAdapter:
    info = AdapterInfo(
        adapter_id="papua_new_guinea_customs_service",
        venue=VENUE,
        market_type="vehicle_tariff_specification_catalog",
        source="Papua New Guinea Customs Service public motor-vehicle TSC implementation notice",
        capabilities=(
            "public_market_data",
            "vehicle_tariff_specification_code",
            "vehicle_model_catalog",
            "engine_code",
            "engine_capacity",
            "model_year_range",
            "customs_classification_reference",
            "source_health",
        ),
        aliases=(
            "papua new guinea customs service",
            "png customs",
            "papua new guinea tscs",
            "tariff specification codes",
            "motor vehicle tsc",
            "toyota vehicle tariff codes",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.papua_new_guinea_customs_service."
            "PapuaNewGuineaCustomsServiceAdapter"
        ),
        quote_assets=(),
        default_cache_minutes=720,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        configured_source = bool(cfg.get("source_url"))
        urls = [source_url]
        if not configured_source and source_url == SOURCE_URL:
            urls.append(TSC_LIST_MIRROR_URL)
        fetch_results: dict[str, dict[str, Any]] = {}
        result: dict[str, Any] | None = None
        selected_url = source_url
        for source in urls:
            fetched = fetch_bytes(source, timeout)
            fetch_results[source] = fetched
            if fetched.get("ok"):
                result = fetched
                selected_url = source
                break
        if result is None:
            result = fetch_results[urls[-1]]
            selected_url = urls[-1]
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [
                _failure_observation(fetch_results[source], source)
                for source in urls
            ]
            statuses = {str(fetched.get("status") or "unavailable") for fetched in fetch_results.values()}
            source_status = "blocked" if "blocked" in statuses else "unavailable"
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                # Text injection supports deterministic tests and a future
                # cached extractor without changing public fetch semantics.
                document: str | bytes = (
                    str(result["text"]) if "text" in result else bytes(result.get("content") or b"")
                )
                observations = parse_papua_new_guinea_customs_tscs(
                    document, source_url=selected_url, received_at=result.get("received_at")
                )
                source_status = "reachable"
                freshness_state = "fresh"
                session_state = observations[0]["session_status"]
            except (PapuaNewGuineaCustomsTscParseError, TypeError, ValueError) as exc:
                message = f"PNG Customs TSC parser failed: {exc}"[:300]
                parser_failures.append({"source_url": selected_url, "error": message})
                observations = [_failure_observation(result, selected_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1042,
                "source_status": source_status,
                "source_url": selected_url,
                "source_urls": urls,
                "fetch_status": {
                    "tsc_list" if source == source_url else "tsc_list_mirror": _fetch_evidence(
                        fetched, source
                    )
                    for source, fetched in fetch_results.items()
                },
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": sum(
                    1 for row in observations if row.get("quality_status") != "source_health"
                ),
                "capability_gap": "public_vehicle_transaction_prices_and_order_routing_not_available",
                "paper_only": True,
            },
        )


register_adapter(PapuaNewGuineaCustomsServiceAdapter())
