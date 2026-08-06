"""TMX public pulses and oilseeds auction-session adapter.

TMX publishes current trade information as a public CSV feed. The 2026/27
trading procedure explicitly covers Green Grams, Sesame seeds, Chick peas,
Pigeon peas, Soy beans, and Bambara nuts under the warehouse-receipt auction
workflow. The public feed does not expose anonymous order routing, so this
adapter emits watch-only paper observations with source-health evidence.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, slug, utc_now
from scan_batch import ScanBatch


MARKET_PAGE_URL = "https://www.tmx.co.tz/page.php?page=market"
MARKET_DATA_URL = "https://www.tmx.co.tz/pages/api/v1/market-data/read_csv.php"
INFODESK_URL = "https://www.tmx.co.tz/page.php?page=infodesk"
GUIDELINES_URL = "https://www.tmx.co.tz/assets/guidelines/2026-2027/guideline_en_27042026.pdf"
TRADING_PROCEDURE_URL = "https://www.tmx.co.tz/assets/guidelines/2026-2027/Pulses_Trading_Procedure__2026_2027.pdf"
VENUE = "TMX"
MARKET_SURFACE = "tanzania_mercantile_exchange_pulses_oilseeds_live_auction_lots_2026_2027"
DAR_TIME = dt.timezone(dt.timedelta(hours=3), name="Africa/Dar_es_Salaam")
SEASON_START = dt.date(2026, 1, 1)

_COMMODITY_SPECS = {
    "bambara nuts": {
        "base": "BAMBARA_NUTS",
        "asset_class": "pulse",
        "group": "pulses",
        "canonical_name": "Bambara nuts",
        "symbol": "BN",
    },
    "chick peas": {
        "base": "CHICK_PEAS",
        "asset_class": "pulse",
        "group": "pulses",
        "canonical_name": "Chick peas",
        "symbol": "CP",
    },
    "green grams": {
        "base": "GREEN_GRAMS",
        "asset_class": "pulse",
        "group": "pulses",
        "canonical_name": "Green Grams",
        "symbol": "GG",
    },
    "greengrams": {
        "base": "GREEN_GRAMS",
        "asset_class": "pulse",
        "group": "pulses",
        "canonical_name": "Green Grams",
        "symbol": "GG",
    },
    "pigeon peas": {
        "base": "PIGEON_PEAS",
        "asset_class": "pulse",
        "group": "pulses",
        "canonical_name": "Pigeon peas",
        "symbol": "PP",
    },
    "sesame seeds": {
        "base": "SESAME_SEEDS",
        "asset_class": "oilseed",
        "group": "oilseeds",
        "canonical_name": "Sesame Seeds",
        "symbol": "SS",
    },
    "soy beans": {
        "base": "SOY_BEANS",
        "asset_class": "oilseed",
        "group": "oilseeds",
        "canonical_name": "Soy Beans",
        "symbol": "SOY",
    },
    "soybeans": {
        "base": "SOY_BEANS",
        "asset_class": "oilseed",
        "group": "oilseeds",
        "canonical_name": "Soy Beans",
        "symbol": "SOY",
    },
}


class TanzaniaMercantileExchangeTmxParseError(ValueError):
    """Raised when the reachable TMX public feed stops matching expectations."""


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TanzaniaMercantileExchangeTmxParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _trade_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise TanzaniaMercantileExchangeTmxParseError(f"invalid trade date: {value!r}") from exc


def _commodity_spec(name: str, code: str) -> dict[str, str] | None:
    normalized = " ".join(str(name or "").replace("-", " ").split()).casefold()
    spec = _COMMODITY_SPECS.get(normalized)
    if spec is not None:
        return spec
    code_key = str(code or "").strip().upper()
    fallback = {
        "BN": "bambara nuts",
        "CP": "chick peas",
        "GG": "green grams",
        "PP": "pigeon peas",
        "SOY": "soy beans",
        "SS": "sesame seeds",
    }.get(code_key)
    return _COMMODITY_SPECS.get(fallback or "")


def _freshness_state(
    trade_date: dt.date,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> tuple[str, float]:
    published_at = dt.datetime.combine(trade_date, dt.time.min, tzinfo=DAR_TIME).astimezone(dt.timezone.utc)
    age_seconds = max(0.0, (fetched_at - published_at).total_seconds())
    freshness = "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400.0 else "stale"
    return freshness, round(age_seconds, 3)


def parse_tanzania_mercantile_exchange_market_csv(
    document: str,
    *,
    source_url: str = MARKET_DATA_URL,
    received_at: str | None = None,
    stale_after_days: float = 14.0,
    season_start: dt.date = SEASON_START,
) -> list[dict[str, Any]]:
    """Normalize TMX public trade rows for the 2026/27 pulses and oilseeds season."""

    text = str(document or "").strip()
    if not text:
        raise TanzaniaMercantileExchangeTmxParseError("TMX market-data CSV is empty")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    required = {
        "Commodity",
        "Code",
        "Location",
        "High Price (TZS/kg)",
        "Low Price (TZS/kg)",
        "Date",
        "Price Change (TZS/kg)",
        "ID",
        "Volume",
    }
    missing = sorted(required - fieldnames)
    if missing:
        raise TanzaniaMercantileExchangeTmxParseError(f"TMX market-data CSV is missing columns: {missing}")
    fetched_at = _received_at(received_at)
    observations: list[dict[str, Any]] = []
    for raw in reader:
        commodity_name = str(raw.get("Commodity") or "").strip()
        code = str(raw.get("Code") or "").strip().upper()
        spec = _commodity_spec(commodity_name, code)
        if spec is None:
            continue
        trade_date = _trade_date(str(raw.get("Date") or ""))
        if trade_date < season_start:
            continue
        high_price = number(raw.get("High Price (TZS/kg)"))
        low_price = number(raw.get("Low Price (TZS/kg)"))
        if high_price is None and low_price is None:
            continue
        last = high_price if high_price not in (None, 0) else low_price
        if last is None or last <= 0:
            continue
        location = " ".join(str(raw.get("Location") or "").split())
        row_id = str(raw.get("ID") or "").strip()
        freshness_state, freshness_age_seconds = _freshness_state(
            trade_date,
            fetched_at,
            stale_after_days,
        )
        observed_at = dt.datetime.combine(trade_date, dt.time.min, tzinfo=DAR_TIME).isoformat()
        published_volume_kg = number(raw.get("Volume"))
        price_change = number(raw.get("Price Change (TZS/kg)"))
        observations.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:TRADE:{code}:{slug(location)}:{trade_date.isoformat()}:{row_id or 'ROW'}",
                "instrument_id": f"{VENUE}:TRADE:{code}:{slug(location)}:{trade_date.isoformat()}:{row_id or 'ROW'}",
                "symbol": f"TMX_{spec['symbol']}_{slug(location)}",
                "name": f"TMX {spec['canonical_name']} {location} trade row",
                "base": spec["base"],
                "quote": "TZS_PER_KG",
                "market_type": "pulses_oilseeds_auction_session_result",
                "market_surface": MARKET_SURFACE,
                "asset_class": spec["asset_class"],
                "trade_type": "official_live_auction_session_row",
                "direction": "watch_only",
                "last": last,
                "price_available": True,
                "price_basis": "published_session_high_price_tzs_per_kg"
                if high_price not in (None, 0)
                else "published_session_low_price_tzs_per_kg",
                "published_high_price_tzs_per_kg": high_price,
                "published_low_price_tzs_per_kg": low_price,
                "published_price_spread_tzs_per_kg": (
                    round(high_price - low_price, 6)
                    if high_price is not None and low_price is not None
                    else None
                ),
                "published_price_change_tzs_per_kg": price_change,
                "published_volume_kg": published_volume_kg,
                "tmx_market_row_id": int(row_id) if row_id.isdigit() else row_id or None,
                "commodity_name": spec["canonical_name"],
                "commodity_code": code,
                "commodity_group": spec["group"],
                "location": location,
                "trade_date": trade_date.isoformat(),
                "season": "2026/27",
                "source_granularity": "location_session_row",
                "lot_level_disclosure_available": False,
                "data_access_type": "public_no_key",
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_auction_session_result",
                "freshness_state": freshness_state,
                "freshness_basis": "official_trade_date",
                "freshness_age_seconds": freshness_age_seconds,
                "session_status": "completed",
                "observed_at": observed_at,
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Tanzania Mercantile Exchange public commodity trade feed",
                "source_url": source_url,
                "source_page_url": MARKET_PAGE_URL,
                "guidelines_url": GUIDELINES_URL,
                "trading_procedure_url": TRADING_PROCEDURE_URL,
                "paper_route_status": "synthetic_research_only",
                "execution_route_status": "route_needed",
                "paper_experiment_eligible": True,
            }
        )
    if not observations:
        raise TanzaniaMercantileExchangeTmxParseError(
            "TMX market-data CSV contained no season-eligible pulses or oilseeds rows"
        )
    return observations


parse_tmx_market_csv = parse_tanzania_mercantile_exchange_market_csv


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
    result: dict[str, Any],
    source_url: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:MARKET_DATA:HEALTH",
            "instrument_id": f"{VENUE}:MARKET_DATA:HEALTH",
            "symbol": "MARKET_DATA_HEALTH",
            "base": "PULSES_OILSEEDS_REFERENCE",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "source_page_url": MARKET_PAGE_URL,
            "guidelines_url": GUIDELINES_URL,
            "trading_procedure_url": TRADING_PROCEDURE_URL,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": (
                "public_tmx_parser_failure" if parser_error else "public_tmx_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {
        **((root.get("adapters") or {}).get(adapter_id) or {}),
        **(root.get(adapter_id) or {}),
    }


class TanzaniaMercantileExchangeTmxAdapter:
    info = AdapterInfo(
        adapter_id="tanzania_mercantile_exchange_tmx_pulses_oilseeds",
        venue=VENUE,
        market_type="pulses_oilseeds_auction_session_result",
        source="Tanzania Mercantile Exchange public pulses and oilseeds trade feed",
        capabilities=(
            "public_market_data",
            "auction_results",
            "event_price_reference",
            "delayed_quote",
            "trade_volume",
            "price_range",
            "source_health",
        ),
        aliases=(
            "tanzania mercantile exchange",
            "tanzania mercantile exchange tmx",
            "tmx tanzania",
            "tmx pulses oilseeds",
            "tanzania pulses auction",
            "tanzania sesame auction",
        ),
        docs_url=INFODESK_URL,
        runtime_entrypoint="adapters.venues.tanzania_mercantile_exchange_tmx.TanzaniaMercantileExchangeTmxAdapter",
        quote_assets=("TZS_PER_KG",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 14.0)))
        source_url = str(cfg.get("market_data_url") or MARKET_DATA_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_tanzania_mercantile_exchange_market_csv(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                source_status = "reachable"
                freshness_states = {str(row.get("freshness_state") or "unknown") for row in observations}
                freshness_state = (
                    next(iter(freshness_states)) if len(freshness_states) == 1 else "mixed"
                )
                latest_trade_date = max(
                    dt.date.fromisoformat(str(row["trade_date"]))
                    for row in observations
                    if row.get("trade_date")
                )
                fetched_at = _received_at(result.get("received_at"))
                age_days = (fetched_at.astimezone(DAR_TIME).date() - latest_trade_date).days
                session_state = (
                    "recent_completed_sessions"
                    if age_days <= max(0, int(cfg.get("recent_session_days", 7)))
                    else "historical_completed_sessions"
                )
            except (TanzaniaMercantileExchangeTmxParseError, TypeError, ValueError) as exc:
                message = f"TMX market-data parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"
        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        commodity_count = len({str(row.get("base") or "") for row in real_rows})
        location_count = len({str(row.get("location") or "") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1303,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [
                    source_url,
                    MARKET_PAGE_URL,
                    INFODESK_URL,
                    GUIDELINES_URL,
                    TRADING_PROCEDURE_URL,
                ],
                "fetch_status": {"market_data_csv": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "commodity_count": commodity_count,
                "location_count": location_count,
                "capability_gap": "public_order_book_buyer_identity_sales_catalogue_and_order_routing_not_available",
                "paper_only": True,
                "season": "2026/27",
            },
        )


TanzaniaMercantileExchangeAdapter = TanzaniaMercantileExchangeTmxAdapter
register_adapter(TanzaniaMercantileExchangeTmxAdapter())
