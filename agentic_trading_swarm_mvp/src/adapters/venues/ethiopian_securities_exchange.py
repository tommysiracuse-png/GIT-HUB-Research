"""Official Ethiopian Securities Exchange equity-listing observations.

The ESX listed-companies page is public and requires no API key.  It provides
issuer identities and listing dates, not executable or entry-quality prices,
so every successful observation remains watch-only.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, utc_now
from scan_batch import ScanBatch


LISTED_COMPANIES_URL = "https://esx.et/equity-market/listed-companies/"
# Conventional alias used by the other single-endpoint venue adapters.
SOURCE_URL = LISTED_COMPANIES_URL
OVERVIEW_URL = "https://esx.et/about/overview/"
ABAY_LISTING_URL = (
    "https://esx.et/ethiopian-securities-exchange-announces-the-official-listing-"
    "of-abay-bank-s-c-on-the-esx-main-market/"
)
MARKET_SURFACE = "ethiopian_securities_exchange_equity_listings"
ADDIS_ABABA_TIME = dt.timezone(dt.timedelta(hours=3))

# Adapter spec 1295 names these newly listed equities.  Older companies on the
# same directory are deliberately not inferred into the requested surface.
REQUIRED_LISTINGS = {
    "BOAX": "Bank of Abyssinia Share Company",
    "ABAYB": "Abay Bank Share Company",
    "TELE": "Ethio Telecom Share Company",
    "AWAB": "Awash Bank Share Company",
}


class EsxEquityListingsParseError(ValueError):
    """Raised when the reachable ESX directory no longer matches its schema."""


def _column(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _parse_date(value: Any, field: str, symbol: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise EsxEquityListingsParseError(
            f"{symbol} has invalid {field}: {value!r}"
        ) from exc


def _parse_received_at(value: str | None) -> dt.datetime:
    if not value:
        value = utc_now()
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EsxEquityListingsParseError(
            "received_at is not an ISO-8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def parse_esx_equity_listings(
    document: str,
    *,
    source_url: str = LISTED_COMPANIES_URL,
    received_at: str | None = None,
    stale_after_days: float = 45.0,
) -> list[dict[str, Any]]:
    """Normalize the four equity listings named by public adapter spec 1295."""

    if not isinstance(document, str) or not document.strip():
        raise EsxEquityListingsParseError("listed-companies response is empty")

    required_headers = {
        "name",
        "symbol",
        "sector",
        "date listed",
        "date incorporated",
    }
    listing_table: list[list[str]] | None = None
    header_indexes: dict[str, int] = {}
    for table in html_tables(document):
        if not table:
            continue
        indexes = {_column(label): index for index, label in enumerate(table[0])}
        if required_headers <= indexes.keys():
            listing_table = table
            header_indexes = indexes
            break
    if listing_table is None:
        raise EsxEquityListingsParseError(
            "listed-companies table with required headers was not found"
        )

    by_symbol: dict[str, list[str]] = {}
    for cells in listing_table[1:]:
        symbol_index = header_indexes["symbol"]
        if symbol_index >= len(cells):
            continue
        symbol = str(cells[symbol_index]).strip().upper()
        if symbol in REQUIRED_LISTINGS:
            if symbol in by_symbol:
                raise EsxEquityListingsParseError(
                    f"listed-companies table contains duplicate symbol {symbol}"
                )
            by_symbol[symbol] = cells

    missing = [symbol for symbol in REQUIRED_LISTINGS if symbol not in by_symbol]
    if missing:
        raise EsxEquityListingsParseError(
            "listed-companies table is missing required symbols: " + ", ".join(missing)
        )

    fetched_at = _parse_received_at(received_at)
    local_fetch_date = fetched_at.astimezone(ADDIS_ABABA_TIME).date()
    observations: list[dict[str, Any]] = []
    for symbol, expected_name in REQUIRED_LISTINGS.items():
        cells = by_symbol[symbol]
        if max(header_indexes.values()) >= len(cells):
            raise EsxEquityListingsParseError(f"{symbol} listing row is incomplete")
        name = str(cells[header_indexes["name"]]).strip()
        if _column(name) != _column(expected_name):
            raise EsxEquityListingsParseError(
                f"{symbol} issuer name changed: expected {expected_name!r}, found {name!r}"
            )
        sector = str(cells[header_indexes["sector"]]).strip()
        listed_date = _parse_date(cells[header_indexes["date listed"]], "Date Listed", symbol)
        incorporated_date = _parse_date(
            cells[header_indexes["date incorporated"]], "Date Incorporated", symbol
        )
        session_status = "listed" if local_fetch_date >= listed_date else "pre_listing"
        age_seconds = max(0.0, (local_fetch_date - listed_date).total_seconds())
        freshness_state = (
            "fresh"
            if age_seconds <= max(0.0, float(stale_after_days)) * 86400.0
            else "stale"
        )
        listed_at = dt.datetime.combine(
            listed_date, dt.time.min, tzinfo=ADDIS_ABABA_TIME
        ).isoformat()
        inst_id = f"ESX:EQUITY:{symbol}"
        observations.append(
            {
                "venue": "ESX",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": symbol,
                "name": name,
                "base": symbol,
                "quote": "ETB",
                "market_type": "cash_equity_reference",
                "market_surface": MARKET_SURFACE,
                "asset_class": "equity",
                "trade_type": "official_listing_directory",
                "direction": "watch_only",
                "last": 0.0,
                "sector": sector,
                "listed_date": listed_date.isoformat(),
                "listed_at": listed_at,
                "incorporated_date": incorporated_date.isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_listing_identity",
                "freshness_state": freshness_state,
                "freshness_basis": "official_listing_date",
                "freshness_age_seconds": round(age_seconds, 3),
                "session_status": session_status,
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Ethiopian Securities Exchange listed companies",
                "source_url": source_url,
                "candidate_reject_reason": (
                    "official_listing_directory_not_entry_quality_quote"
                ),
            }
        )
    return observations


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
    observation = health_observation("ESX", source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_listing_parser_failure"
                if parser_error
                else "public_listing_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class EthiopianSecuritiesExchangeAdapter:
    info = AdapterInfo(
        adapter_id="ethiopian_securities_exchange",
        venue="ESX",
        market_type="cash_equity_reference",
        source="Ethiopian Securities Exchange official listed companies",
        capabilities=(
            "public_market_data",
            "catalog",
            "equity_listing_catalog",
            "issuer_identity",
            "listing_schedule",
            "source_health",
        ),
        aliases=(
            "ethiopian securities exchange",
            "ethiopia securities exchange",
            "esx",
            "bank of abyssinia",
            "abay bank",
            "ethio telecom",
            "awash bank",
            "boax",
            "abayb",
            "tele",
            "awab",
        ),
        docs_url=LISTED_COMPANIES_URL,
        runtime_entrypoint=(
            "adapters.venues.ethiopian_securities_exchange."
            "EthiopianSecuritiesExchangeAdapter"
        ),
        quote_assets=("ETB",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 45.0)))
        source_url = str(cfg.get("source_url") or LISTED_COMPANIES_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_esx_equity_listings(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                freshness_states = {
                    str(row["freshness_state"]) for row in observations
                }
                session_states = {str(row["session_status"]) for row in observations}
                source_status = "reachable"
                freshness_state = (
                    "fresh"
                    if "fresh" in freshness_states
                    else next(iter(freshness_states), "unknown")
                )
                session_state = (
                    next(iter(session_states)) if len(session_states) == 1 else "mixed"
                )
            except (EsxEquityListingsParseError, TypeError, ValueError) as exc:
                message = f"ESX equity-listings parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        source_urls = list(
            dict.fromkeys([source_url, LISTED_COMPANIES_URL, OVERVIEW_URL, ABAY_LISTING_URL])
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1295,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": source_urls,
                "fetch_status": {"listed_companies": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "freshness_states": sorted(
                    {str(row.get("freshness_state") or "unknown") for row in observations}
                ),
                "session_state": session_state,
                "session_states": sorted(
                    {str(row.get("session_status") or "unknown") for row in observations}
                ),
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_quotes",
                "paper_only": True,
            },
        )


register_adapter(EthiopianSecuritiesExchangeAdapter())
