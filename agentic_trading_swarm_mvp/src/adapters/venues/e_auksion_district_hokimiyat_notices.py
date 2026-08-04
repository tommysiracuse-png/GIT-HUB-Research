"""Public E-auksion land-lot adapter sourced from district hokimiyat notices.

The platform rows are official auction references, not executable securities or
broker quotes.  They remain watch-only even while applications or bidding are
open so they cannot become an order route in paper mode.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number
from scan_batch import ScanBatch


API_URL = "https://e-auksion.uz/api/front/lots"
NOTICE_URL = "https://gov.uz/oz/taqiyatas/sections/view/74515"
POLICY_URL = "https://gov.uz/ru/advice/59/document/2094"
LOT_URL = "https://e-auksion.uz/lot-view?lot_id={}"
UZBEKISTAN_TIME = dt.timezone(dt.timedelta(hours=5))
MARKET_SURFACE = "uzbekistan_e_auksion_entrepreneurship_land_leases"


class EAuksionParseError(ValueError):
    """Raised when the reachable public lot feed no longer matches its schema."""


def _time(value: Any, *, timezone: dt.tzinfo = dt.timezone.utc) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text.replace("Z", "+0000"), pattern)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone)


def _received_time(value: str | None) -> dt.datetime:
    return _time(value) or dt.datetime.now(dt.timezone.utc)


def _session_state(
    received_at: dt.datetime,
    order_deadline: dt.datetime | None,
    auction_at: dt.datetime,
    lot_status_id: int,
) -> str:
    comparable = received_at.astimezone(auction_at.tzinfo)
    if order_deadline and comparable < order_deadline:
        return "applications_open"
    if comparable < auction_at:
        return "applications_closed"
    if lot_status_id == 11:
        return "auction_live"
    return "auction_elapsed"


def _geography(address: str) -> tuple[str | None, str | None, str | None]:
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    return (
        parts[0] if parts else None,
        parts[1] if len(parts) > 1 else None,
        ", ".join(parts[2:]) if len(parts) > 2 else None,
    )


def parse_e_auksion_lots(
    payload: str | dict[str, Any],
    *,
    source_url: str = API_URL,
    received_at: str | None = None,
    stale_after_hours: float = 24.0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Normalize official public E-auksion entrepreneurship land-lot rows."""

    if isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EAuksionParseError(f"invalid JSON response: {exc.msg}") from exc
    elif isinstance(payload, dict):
        document = payload
    else:
        raise EAuksionParseError("lot response must be a JSON object")

    rows = document.get("rows")
    if not isinstance(rows, list):
        raise EAuksionParseError("JSON response is missing the rows array")

    fetched = _received_time(received_at)
    parsed: list[dict[str, Any]] = []
    invalid_rows = 0
    for raw in rows:
        if not isinstance(raw, dict):
            invalid_rows += 1
            continue
        lot_number = str(raw.get("lot_number") or raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        address = str(raw.get("full_address") or "").strip()
        start_price = number(raw.get("start_price"))
        auction_at = _time(raw.get("auction_date_str"), timezone=UZBEKISTAN_TIME)
        if not lot_number or not name or not address or start_price is None or start_price <= 0 or auction_at is None:
            invalid_rows += 1
            continue

        order_deadline = _time(raw.get("order_end_time_str"), timezone=UZBEKISTAN_TIME)
        lot_status_id = int(number(raw.get("lot_statuses_id")) or 0)
        elapsed = max(0.0, (fetched - auction_at.astimezone(dt.timezone.utc)).total_seconds())
        freshness_state = "fresh" if elapsed <= max(0.0, stale_after_hours) * 3600.0 else "stale"
        region, district, locality = _geography(address)
        assessed_value = number(raw.get("baholangan_narx"))
        price_ratio = start_price / assessed_value if assessed_value and assessed_value > 0 else None
        lot_url = LOT_URL.format(str(raw.get("id") or lot_number).strip())
        fetched_at = received_at or fetched.isoformat()

        parsed.append(
            {
                "venue": "E_AUKSION_UZ",
                "inst_id": f"E_AUKSION_UZ:LAND_LEASE:{lot_number}",
                "instrument_id": f"E_AUKSION_UZ:LAND_LEASE:{lot_number}",
                "symbol": f"LAND_LEASE_{lot_number}",
                "name": name,
                "base": f"LAND_LEASE_{lot_number}",
                "quote": "UZS",
                "market_type": "land_lease_auction",
                "market_surface": MARKET_SURFACE,
                "asset_class": "land_lease_right",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": start_price,
                "starting_price_uzs": start_price,
                "deposit_uzs": number(raw.get("zaklad_summa")),
                "deposit_percent": number(raw.get("zaklad_percent")),
                "assessed_value_uzs": assessed_value,
                "starting_price_to_assessed_ratio": round(price_ratio, 6) if price_ratio is not None else None,
                "lot_number": lot_number,
                "lot_status_id": lot_status_id,
                "category_id": int(number(raw.get("category_id")) or 0),
                "category_name": str(raw.get("confiscant_categories_name") or "").strip() or None,
                "address": address,
                "region": region,
                "district": district,
                "locality": locality,
                "auction_at": auction_at.isoformat(),
                "application_deadline": order_deadline.isoformat() if order_deadline else None,
                "installment_available": number(raw.get("is_term_payment")) == 1,
                "payment_terms_months": int(number(raw.get("term_month")) or 0) or None,
                "application_count": int(number(raw.get("user_orders_apply_cnt")) or 0),
                "view_count": int(number(raw.get("view_count")) or 0),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_platform_listing",
                "freshness_state": freshness_state,
                "freshness_basis": "official_auction_schedule",
                "freshness_age_seconds": round(elapsed, 3),
                "session_status": _session_state(fetched, order_deadline, auction_at, lot_status_id),
                "observed_at": fetched_at,
                "fetched_at": fetched_at,
                "price_source": "E-auksion public lot feed",
                "source_url": source_url,
                "source_notice_url": NOTICE_URL,
                "policy_url": POLICY_URL,
                "lot_url": lot_url,
                "candidate_reject_reason": "land_lease_auction_not_order_routable",
            }
        )

    if not parsed:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise EAuksionParseError(f"no usable entrepreneurship land-lot rows{detail}")
    return parsed[: max(1, int(limit))]


def _request_payload(cfg: dict[str, Any], limit: int) -> dict[str, Any]:
    return {
        "sort_type": 1,
        "confiscant_groups_id": int(cfg.get("group_id", 6)),
        "confiscant_categories_id": int(cfg.get("category_id", 46)),
        "regions_id": cfg.get("region_id"),
        "areas_id": cfg.get("area_id"),
        "mahallas_id": None,
        "address": None,
        "lot_number": None,
        "hashtag": None,
        "date_from": None,
        "date_to": None,
        "auction_date": None,
        "is_term_order": None,
        "exec_order_type": None,
        "lot_type": 0,
        "auction_type": 0,
        "finished_auction_status": None,
        "filtered_auction_status": None,
        "is_ownership": None,
        "orderby_": int(cfg.get("order", 0)),
        "current_page": max(1, int(cfg.get("page", 1))),
        "per_page": limit,
        "dynamic_filters": [],
        "bank_id": None,
    }


def _failure_observation(result: dict[str, Any], parser_error: str | None = None) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("E_AUKSION_UZ", API_URL, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_notice_url": NOTICE_URL,
            "policy_url": POLICY_URL,
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


class EAuksionDistrictHokimiyatNoticesAdapter:
    info = AdapterInfo(
        adapter_id="e_auksion_district_hokimiyat_notices",
        venue="E_AUKSION_UZ",
        market_type="land_lease_auction",
        source="Uzbekistan E-auksion public lots and district hokimiyat notice",
        capabilities=(
            "auction_schedule",
            "application_deadline",
            "starting_price",
            "deposit_terms",
            "geography",
            "land_lease_lots",
            "source_health",
        ),
        aliases=(
            "e-auksion",
            "e-ijro auksion",
            "uzbekistan land auction",
            "district hokimiyat land notices",
            "qoraqalpogiston land leases",
        ),
        docs_url=NOTICE_URL,
        runtime_entrypoint=(
            "adapters.venues.e_auksion_district_hokimiyat_notices."
            "EAuksionDistrictHokimiyatNoticesAdapter"
        ),
        quote_assets=("UZS",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        limit = max(1, min(int(cfg.get("max_rows", 100)), 500))
        stale_after_hours = max(0.0, float(cfg.get("stale_after_hours", 24.0)))
        result = fetch_text(
            API_URL,
            timeout,
            method="POST",
            json_body=_request_payload(cfg, limit),
        )
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_e_auksion_lots(
                    result.get("text") or "",
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                )
                source_status = "reachable"
            except (EAuksionParseError, TypeError, ValueError) as exc:
                message = f"E-auksion lot parser failed: {exc}"[:300]
                parser_failures.append({"source_url": API_URL, "error": message})
                observations = [_failure_observation(result, message)]
                source_status = "degraded"

        freshness_states = {str(row.get("freshness_state") or "unknown") for row in observations}
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 607,
                "source_status": source_status,
                "source_urls": [API_URL, NOTICE_URL, POLICY_URL],
                "fetch_status": {
                    "lots": {
                        "source_url": API_URL,
                        "fetch_status": str(result.get("status") or "unavailable"),
                        "http_status": result.get("http_status"),
                        "fetched_at": result.get("received_at"),
                        "latency_ms": result.get("latency_ms"),
                    }
                },
                "freshness_state": freshness_state,
                "session_state": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "paper_only": True,
            },
        )


register_adapter(EAuksionDistrictHokimiyatNoticesAdapter())
