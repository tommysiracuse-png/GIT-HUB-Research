"""Virginia DEQ NPS nutrient-credit registry adapter.

Virginia DEQ publishes the NPS nutrient-bank application list as a public
ArcGIS feature layer.  It is useful evidence of credit availability and status
transitions, but is not an order-entry venue.  This adapter therefore keeps
all records and source-health evidence watch-only and paper-research-only.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


NUTRIENT_TRADING_URL = "https://www.deq.virginia.gov/news-info/shortcuts/permits/water/nutrient-trading"
REGULATION_URL = "https://law.lis.virginia.gov/admincodefull/title9/agency25/chapter900/"
MAP_SERVER_URL = "https://gisdata.deq.virginia.gov/arcgis/rest/services/public/NPSNutrientTrading/MapServer"
BANK_LAYER_URL = f"{MAP_SERVER_URL}/0"
BANK_QUERY_URL = (
    f"{BANK_LAYER_URL}/query?where=1%3D1&outFields="
    "WQT_PROJECT_ID%2CWQT_PROJECT_NAME%2CLONGITUDE%2CLATITUDE%2C"
    "PROJECT_STATUS_NAME%2CPROJECT_CAT_NAME%2CSTATE_ABBREV_LIST%2CNUM_SA%2C"
    "TOTAL_AVAIL_PHOS%2CTOTAL_PEND_OF_PHOS%2CTOTAL_POTENTIAL_OF_PHOS&"
    "returnGeometry=false&f=json"
)

VENUE = "VA_DEQ_NPS_NUTRIENT"
MARKET_SURFACE = "virginia_nps_nutrient_credit_registry"
REQUIRED_FIELDS = {
    "WQT_PROJECT_ID",
    "WQT_PROJECT_NAME",
    "PROJECT_STATUS_NAME",
    "TOTAL_AVAIL_PHOS",
}


class VirginiaNpsNutrientRegistryParseError(ValueError):
    """Raised when the reachable public ArcGIS registry changes schema."""


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VirginiaNpsNutrientRegistryParseError(
            "received_at is not an ISO-8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _registry_number(value: Any) -> float | None:
    """Parse ArcGIS numerics while retaining an explicit numeric zero."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    return number(value)


def _huc_from_project_name(project_name: str) -> str | None:
    """Keep a HUC only when the registry itself includes one in the name."""

    match = re.search(r"(?<!\d)(\d{8,12})(?!\d)", project_name)
    return match.group(1) if match else None


def _state_abbreviations(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[,;/]", _text(value)) if part.strip()]


def _as_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise VirginiaNpsNutrientRegistryParseError("invalid ArcGIS JSON response") from exc
    if not isinstance(payload, dict):
        raise VirginiaNpsNutrientRegistryParseError("ArcGIS response is not a JSON object")
    error = payload.get("error")
    if error:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise VirginiaNpsNutrientRegistryParseError(f"ArcGIS query error: {_text(detail) or 'unknown'}")
    features = payload.get("features")
    if not isinstance(features, list):
        raise VirginiaNpsNutrientRegistryParseError("ArcGIS response has no features list")
    declared_fields = {
        _text(field.get("name"))
        for field in payload.get("fields", [])
        if isinstance(field, dict) and _text(field.get("name"))
    }
    if declared_fields and not REQUIRED_FIELDS.issubset(declared_fields):
        missing = sorted(REQUIRED_FIELDS - declared_fields)
        raise VirginiaNpsNutrientRegistryParseError(
            "ArcGIS layer missing required fields: " + ", ".join(missing)
        )
    return payload


def parse_virginia_nps_nutrient_banks(
    payload: str | dict[str, Any],
    *,
    source_url: str = BANK_QUERY_URL,
    received_at: str | None = None,
    limit: int = 1_000,
) -> list[dict[str, Any]]:
    """Normalize the official weekly NPS-bank feature layer.

    Availability is a quantity, not an executable price.  Missing availability
    is deliberately preserved as an unreported registry value rather than
    interpreted as zero or used to suppress the bank from paper research.
    """

    response = _as_payload(payload)
    fetched_at = _received_time(received_at)
    rows: list[dict[str, Any]] = []
    invalid_rows = 0
    for feature in response["features"]:
        attributes = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attributes, dict):
            invalid_rows += 1
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in attributes]
        if missing:
            invalid_rows += 1
            continue
        project_id = _text(attributes.get("WQT_PROJECT_ID"))
        project_name = _text(attributes.get("WQT_PROJECT_NAME"))
        project_status = _text(attributes.get("PROJECT_STATUS_NAME"))
        available_phosphorus_credits = _registry_number(attributes.get("TOTAL_AVAIL_PHOS"))
        if not project_id or not project_name or not project_status:
            invalid_rows += 1
            continue
        pending_phosphorus_credits = _registry_number(attributes.get("TOTAL_PEND_OF_PHOS"))
        potential_phosphorus_credits = _registry_number(attributes.get("TOTAL_POTENTIAL_OF_PHOS"))
        latitude = _registry_number(attributes.get("LATITUDE"))
        longitude = _registry_number(attributes.get("LONGITUDE"))
        service_areas = _registry_number(attributes.get("NUM_SA"))
        huc = _huc_from_project_name(project_name)
        states = _state_abbreviations(attributes.get("STATE_ABBREV_LIST"))
        pending_release = project_status.casefold() == "pending" and (
            (potential_phosphorus_credits or 0.0) > 0.0
            or (available_phosphorus_credits or 0.0) > 0.0
        )
        inst_id = f"{VENUE}:NPS_BANK:{project_id}"
        rows.append(
            {
                "venue": VENUE,
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": "VA_NPS_PHOSPHORUS_CREDIT_BANK",
                "name": f"Virginia NPS nutrient bank {project_name}",
                "base": "VA_NPS_PHOSPHORUS_CREDIT",
                "quote": "PHOSPHORUS_CREDITS",
                "market_type": "nutrient_credit_registry_reference",
                "market_surface": MARKET_SURFACE,
                "asset_class": "nutrient_credit",
                "trade_type": "official_nutrient_credit_registry_reference",
                "direction": "watch_only",
                "last": available_phosphorus_credits,
                "project_id": project_id,
                "project_name": project_name,
                "project_status": project_status,
                "project_category": _text(attributes.get("PROJECT_CAT_NAME")) or None,
                "available_phosphorus_credits": available_phosphorus_credits,
                "available_phosphorus_credits_reported": available_phosphorus_credits is not None,
                "pending_phosphorus_credits": pending_phosphorus_credits,
                "potential_phosphorus_credits": potential_phosphorus_credits,
                "service_area_count": service_areas,
                "state_abbreviations": states,
                "latitude": latitude,
                "longitude": longitude,
                "geography": {
                    "state_abbreviations": states,
                    "latitude": latitude,
                    "longitude": longitude,
                    "huc": huc,
                },
                "huc": huc,
                "pending_release_watch": pending_release,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_weekly_nps_nutrient_bank_registry",
                "freshness_state": "fresh",
                "freshness_basis": "weekly_public_registry_snapshot_fetch",
                "freshness_age_seconds": 0.0,
                "session_status": "public_registry_snapshot",
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Virginia DEQ NPS Nutrient Trading MapServer",
                "source_url": source_url,
                "source_layer_url": BANK_LAYER_URL,
                "source_map_server_url": MAP_SERVER_URL,
                "source_docs_url": NUTRIENT_TRADING_URL,
                "source_regulation_url": REGULATION_URL,
                "candidate_reject_reason": "public_nutrient_credit_registry_not_order_routable",
                "paper_route_status": "synthetic_research_only",
            }
        )
    if not rows:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise VirginiaNpsNutrientRegistryParseError(f"no usable NPS nutrient-bank rows{detail}")
    return rows[: max(1, int(limit))]


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": _text(result.get("error"))[:300] or None,
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
            "inst_id": f"{VENUE}:NPS_BANKS:HEALTH",
            "instrument_id": f"{VENUE}:NPS_BANKS:HEALTH",
            "symbol": "VA_NPS_NUTRIENT_BANKS_HEALTH",
            "base": "VA_NPS_PHOSPHORUS_CREDIT",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "candidate_reject_reason": (
                "public_nutrient_credit_registry_parser_failure"
                if parser_error
                else "public_nutrient_credit_registry_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class VirginiaDeqNpsNutrientCreditRegistryAdapter:
    info = AdapterInfo(
        adapter_id="virginia_deq_nps_nutrient_credit_registry",
        venue=VENUE,
        market_type="nutrient_credit_registry_reference",
        source="Virginia DEQ public NPS Nutrient Credit Registry / Application List",
        capabilities=(
            "public_market_data",
            "nutrient_credit_registry",
            "nps_nutrient_banks",
            "phosphorus_credit_availability",
            "project_status",
            "geography",
            "status_transition_watch",
            "source_health",
        ),
        aliases=(
            "virginia deq",
            "virginia nps nutrient credit registry",
            "nps nutrient credit registry",
            "nps nutrient trading",
            "virginia nutrient banks",
            "virginia nutrient trading",
        ),
        docs_url=NUTRIENT_TRADING_URL,
        runtime_entrypoint=(
            "adapters.venues.virginia_deq_nps_nutrient_credit_registry."
            "VirginiaDeqNpsNutrientCreditRegistryAdapter"
        ),
        quote_assets=("PHOSPHORUS_CREDITS",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        source_url = str(cfg.get("source_url") or cfg.get("registry_url") or BANK_QUERY_URL)
        limit = max(1, int(cfg.get("limit", 1_000)))
        result = fetch_text(source_url, timeout)
        fetch_status = {"nps_banks": _fetch_evidence(result, source_url)}
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_virginia_nps_nutrient_banks(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    limit=limit,
                )
                source_status = "reachable"
            except (VirginiaNpsNutrientRegistryParseError, TypeError, ValueError) as exc:
                message = f"Virginia DEQ NPS registry parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1250,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [
                    source_url,
                    BANK_LAYER_URL,
                    MAP_SERVER_URL,
                    NUTRIENT_TRADING_URL,
                    REGULATION_URL,
                ],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "broker_listing_prices_tributary_eligibility_and_order_routing",
                "paper_only": True,
            },
        )


register_adapter(VirginiaDeqNpsNutrientCreditRegistryAdapter())
