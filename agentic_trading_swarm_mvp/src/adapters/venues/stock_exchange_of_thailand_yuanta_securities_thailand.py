"""Official SET listing observations for Yuanta's July 2026 DR batch.

The SET announcement is public and requires no API key.  It supplies verified
listing identities and dates, not entry-quality quotes, so successful rows are
real reference observations but remain watch-only and cannot become orders.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation
from scan_batch import ScanBatch


SOURCE_URL = (
    "https://www.set.or.th/en/market/news-and-alert/newsdetails?"
    "id=105334000&symbol=SET&t=SET+News+%3A11+New+DRs+referencing+securities+"
    "across+Asia%2C+the+U.S.%2C+and+Europe+issued+by+Yuanta+to+start+trading+on+July+7"
)
MARKET_SURFACE = "set_yuanta_thailand_depositary_receipts"
THAILAND_TIME = dt.timezone(dt.timedelta(hours=7))

# Symbol, underlying symbol, and underlying listing venue.  Company/fund names
# are parsed from the announcement so upstream schema changes cannot be hidden
# by a fully hard-coded catalog.
DR_REFERENCES = (
    ("BABA19", "9988", "HKEX"),
    ("SHINCHEM19", "4063", "TSE"),
    ("AMAT19", "AMAT", "NASDAQ"),
    ("AMZN19", "AMZN", "NASDAQ"),
    ("GOOGL19", "GOOGL", "NASDAQ"),
    ("INTEL19", "INTC", "NASDAQ"),
    ("KLAC19", "KLAC", "NASDAQ"),
    ("LRCX19", "LRCX", "NASDAQ"),
    ("PANW19", "PANW", "NASDAQ"),
    ("CAT19", "CAT", "NYSE"),
    ("DEAM19", "DEAM", "DEUTSCHE_BOERSE"),
)


class SetYuantaParseError(ValueError):
    """Raised when the reachable announcement no longer matches its schema."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_text(document: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
    except Exception as exc:  # noqa: BLE001 - retain malformed upstream HTML as evidence.
        raise SetYuantaParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _announcement_time(text: str) -> dt.datetime:
    match = re.search(
        r"Date/Time\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        try:
            return dt.datetime.strptime(match.group(1), "%d %b %Y %H:%M:%S").replace(
                tzinfo=THAILAND_TIME
            )
        except ValueError as exc:
            raise SetYuantaParseError("announcement Date/Time is invalid") from exc

    # SET can localize the visible date widget to Thai even on the /en page.
    # Its public page state retains the same announcement timestamp in ISO-8601
    # immediately before the record's symbol field.
    state_match = re.search(
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))'
        r'["\']?\s*,\s*symbol\s*:',
        text,
        flags=re.IGNORECASE,
    )
    if state_match:
        try:
            parsed = dt.datetime.fromisoformat(state_match.group(1).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SetYuantaParseError("announcement Date/Time is invalid") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=THAILAND_TIME)

    raise SetYuantaParseError("announcement Date/Time was not found")


def _commencement_time(text: str) -> dt.datetime:
    match = re.search(
        r"Trading\s+will\s+commence\s+on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise SetYuantaParseError("trading commencement date was not found")
    try:
        date = dt.datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError as exc:
        raise SetYuantaParseError("trading commencement date is invalid") from exc
    return dt.datetime.combine(date, dt.time.min, tzinfo=THAILAND_TIME)


def _received_time(value: str | None, fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SetYuantaParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def parse_set_yuanta_dr_announcement(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_hours: float = 168.0,
) -> list[dict[str, Any]]:
    """Normalize all 11 DR identities from the official SET announcement."""

    if not isinstance(document, str) or not document.strip():
        raise SetYuantaParseError("announcement response is empty")
    text = _visible_text(document)
    if "Yuanta" not in text or "depositary receipts" not in text.lower():
        raise SetYuantaParseError("SET Yuanta DR announcement marker was not found")

    announced_at = _announcement_time(text)
    commences_at = _commencement_time(text)
    fetched_at = _received_time(received_at, announced_at)
    age_seconds = max(
        0.0,
        (
            fetched_at.astimezone(dt.timezone.utc)
            - announced_at.astimezone(dt.timezone.utc)
        ).total_seconds(),
    )
    freshness_state = (
        "fresh"
        if age_seconds <= max(0.0, float(stale_after_hours)) * 3600.0
        else "stale"
    )
    session_status = (
        "listed"
        if fetched_at.astimezone(THAILAND_TIME) >= commences_at
        else "pre_listing"
    )

    observations: list[dict[str, Any]] = []
    missing: list[str] = []
    for symbol, underlying_symbol, underlying_venue in DR_REFERENCES:
        match = re.search(
            rf'["“]?{re.escape(symbol)}["”]?\s+on\s+(?:shares\s+of\s+)?'
            rf'(.+?)\s*\(\s*{re.escape(underlying_symbol)}\s*\)',
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            missing.append(symbol)
            continue
        underlying_name = re.sub(r"\s+", " ", match.group(1)).strip(" -:;,.\"")
        if not underlying_name:
            missing.append(symbol)
            continue
        inst_id = f"SET:DR:{symbol}"
        observations.append(
            {
                "venue": "SET",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": symbol,
                "name": f"{symbol} Depositary Receipt",
                "base": symbol,
                "quote": "THB",
                "market_type": "depositary_receipt",
                "market_surface": MARKET_SURFACE,
                "asset_class": "depositary_receipt",
                "trade_type": "official_listing_announcement",
                "direction": "watch_only",
                "last": 0.0,
                "issuer": "Yuanta Securities (Thailand) Co., Ltd.",
                "underlying_symbol": underlying_symbol,
                "underlying_name": underlying_name,
                "underlying_venue": underlying_venue,
                "announcement_at": announced_at.isoformat(),
                "trading_commences_at": commences_at.isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_listing_identity",
                "freshness_state": freshness_state,
                "freshness_basis": "SET_announcement_timestamp",
                "freshness_age_seconds": round(age_seconds, 3),
                "session_status": session_status,
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Stock Exchange of Thailand official announcement",
                "source_url": source_url,
                "candidate_reject_reason": (
                    "official_listing_announcement_not_entry_quality_quote"
                ),
            }
        )

    if missing:
        raise SetYuantaParseError(
            "announcement is missing documented DR references: " + ", ".join(missing)
        )
    return observations


def _fetch_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": SOURCE_URL,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(
    result: dict[str, Any], parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("SET", SOURCE_URL, evidence, MARKET_SURFACE)
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


class StockExchangeOfThailandYuantaSecuritiesThailandAdapter:
    info = AdapterInfo(
        adapter_id="stock_exchange_of_thailand_yuanta_securities_thailand",
        venue="SET",
        market_type="depositary_receipt",
        source="Stock Exchange of Thailand official Yuanta DR announcement",
        capabilities=(
            "depositary_receipt_catalog",
            "issuer_identity",
            "underlying_identity",
            "listing_schedule",
            "source_health",
        ),
        aliases=(
            "stock exchange of thailand",
            "set depositary receipts",
            "yuanta securities thailand",
            "thai dr",
            "baba19",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.stock_exchange_of_thailand_yuanta_securities_thailand."
            "StockExchangeOfThailandYuantaSecuritiesThailandAdapter"
        ),
        quote_assets=("THB",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_hours = max(0.0, float(cfg.get("stale_after_hours", 168.0)))
        result = fetch_text(SOURCE_URL, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_set_yuanta_dr_announcement(
                    str(result.get("text") or ""),
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                )
                source_status = "reachable"
                freshness_state = str(observations[0]["freshness_state"])
                session_state = str(observations[0]["session_status"])
            except (SetYuantaParseError, TypeError, ValueError) as exc:
                message = f"SET Yuanta DR parser failed: {exc}"[:300]
                parser_failures.append({"source_url": SOURCE_URL, "error": message})
                observations = [_failure_observation(result, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 959,
                "source_status": source_status,
                "source_urls": [SOURCE_URL],
                "fetch_status": {"announcement": _fetch_evidence(result)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_quotes",
                "paper_only": True,
            },
        )


register_adapter(StockExchangeOfThailandYuantaSecuritiesThailandAdapter())
