"""Public, paper-only KASE Global ETF and depositary-receipt references.

KASE Global publishes a daily listing table for foreign securities.  The table
is useful for monitoring local wrapper/reference-price dislocations, but it is
not an executable market-data feed.  This adapter intentionally emits no
candidates and marks every observation ``watch_only``.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://kase.kz/en/markets/kase-global"
MARKET_MAKER_NOTICE_URL = "https://kase.kz/en/information/news/show/1569465"
DIGITAL_ASSET_ETF_NOTICE_URL = "https://kase.kz/en/information/news/show/1568542"
KASE_TIME = dt.timezone(dt.timedelta(hours=5))

# These are the KASE Global wrappers in adapter spec #1239.  Keeping the
# scope explicit avoids claiming coverage of every foreign security on KASE.
TARGET_SYMBOLS = frozenset(
    {"IBIT_KZ", "ETHA_KZ", "SOLZ_KZ", "BITO_KZ", "SPY_KZ", "QQQ_KZ", "BABAd", "BIDUd"}
)


class KaseGlobalParseError(ValueError):
    """Raised when the reachable KASE Global page does not expose its listing table."""


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _as_of(value: Any) -> dt.datetime | None:
    try:
        date = dt.datetime.strptime(str(value or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    # KASE publishes a daily market reference rather than an intraday event.
    return dt.datetime.combine(date, dt.time(18), tzinfo=KASE_TIME)


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _freshness(as_of: dt.datetime | None, received_at: str | None, stale_after_hours: float) -> tuple[str, float | None]:
    if as_of is None:
        return "unknown", None
    age = max(0.0, (_received_time(received_at).astimezone(dt.timezone.utc) - as_of.astimezone(dt.timezone.utc)).total_seconds())
    return ("fresh" if age <= max(0.0, stale_after_hours) * 3600.0 else "stale"), round(age, 3)


def _listing_table(document: str) -> tuple[list[str], list[list[str]]]:
    for rows in html_tables(document):
        if not rows:
            continue
        header = [_header_key(cell) for cell in rows[0]]
        required = {"ticker", "company", "isin", "type", "currency", "date"}
        if required <= set(header) and any(key.startswith("price") for key in header):
            return header, rows[1:]
    raise KaseGlobalParseError("KASE Global listing table with ticker, ISIN, price, and date headers was not found")


def _column(header: list[str], prefix: str) -> int | None:
    return next((index for index, key in enumerate(header) if key.startswith(prefix)), None)


def _cell(row: list[str], index: int | None) -> str:
    return str(row[index]).strip() if index is not None and index < len(row) else ""


def parse_kase_global(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_hours: float = 72.0,
) -> list[dict[str, Any]]:
    """Normalize the requested KASE Global ETF and ADR daily references."""

    if not isinstance(document, str) or not document.strip():
        raise KaseGlobalParseError("KASE Global response is empty")
    header, rows = _listing_table(document)
    ticker_index = _column(header, "ticker")
    company_index = _column(header, "company")
    isin_index = _column(header, "isin")
    type_index = _column(header, "type")
    currency_index = _column(header, "currency")
    price_index = _column(header, "price")
    volume_index = _column(header, "volume")
    date_index = _column(header, "date")
    liquidity_index = _column(header, "liquidityclass")
    market_maker_index = _column(header, "marketmaker")

    observations: list[dict[str, Any]] = []
    for row in rows:
        symbol = _cell(row, ticker_index)
        if symbol not in TARGET_SYMBOLS:
            continue
        security_type = _cell(row, type_index)
        price = number(_cell(row, price_index))
        as_of = _as_of(_cell(row, date_index))
        freshness_state, freshness_age = _freshness(as_of, received_at, stale_after_hours)
        is_etf = security_type.lower() == "etf"
        has_reference_price = price is not None and price > 0
        observations.append(
            {
                "venue": "KASE",
                "inst_id": f"KASE:{symbol}",
                "instrument_id": f"KASE:{symbol}",
                "symbol": symbol,
                "name": _cell(row, company_index) or symbol,
                "isin": _cell(row, isin_index) or None,
                "security_type": security_type or None,
                "base": symbol,
                "quote": _cell(row, currency_index) or "USD",
                "market_type": "fund" if is_etf else "equity",
                "market_surface": "kase_global_foreign_etfs_and_adrs",
                "asset_class": "foreign_etf" if is_etf else "foreign_depository_receipt",
                "trade_type": "official_delayed_market_reference" if has_reference_price else "official_listing_catalog",
                "direction": "watch_only",
                "last": price if has_reference_price else 0.0,
                "reported_volume_millions_kzt": number(_cell(row, volume_index)),
                "liquidity_class": _cell(row, liquidity_index) or None,
                "market_makers": _cell(row, market_maker_index) or None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "delayed_reference_only" if has_reference_price else "listing_without_trade_reference",
                "freshness_state": freshness_state,
                "freshness_basis": "official_kase_global_daily_date" if as_of else "not_published",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed" if has_reference_price else "no_trade_reported",
                "observed_at": (as_of or _received_time(received_at)).isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "KASE Global official daily listing table",
                "source_url": source_url,
                "candidate_reject_reason": (
                    "official_delayed_reference_not_entry_quality"
                    if has_reference_price
                    else "no_official_trade_reference_for_instrument"
                ),
            }
        )
    if not observations:
        raise KaseGlobalParseError("none of the requested KASE Global ETF or ADR symbols were found")
    return observations


def _source_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
    }


def _failure_observation(source_url: str, result: dict[str, Any], parser_error: str | None = None) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    observation = health_observation("KASE", source_url, evidence, "kase_global_foreign_etfs_and_adrs")
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_kase_global_parser_failure" if parser_error else "public_kase_global_source_unavailable"
            ),
        }
    )
    return observation


class KazakhstanStockExchangeKaseGlobalAdapter:
    info = AdapterInfo(
        adapter_id="kazakhstan_stock_exchange_kase_global",
        venue="KASE",
        market_type="foreign_securities",
        source="KASE Global official public market page",
        capabilities=(
            "public_market_data",
            "ticker_reference",
            "delayed_quote",
            "daily_market_reference",
            "settlement_reference",
            "foreign_etf",
            "foreign_adr",
            "market_maker_reference",
            "source_health",
        ),
        aliases=(
            "kazakhstan stock exchange",
            "kase",
            "kase global",
            "kazakhstan foreign etfs",
            "kazakhstan adrs",
        ),
        docs_url=MARKET_MAKER_NOTICE_URL,
        runtime_entrypoint=(
            "adapters.venues.kazakhstan_stock_exchange_kase."
            "KazakhstanStockExchangeKaseGlobalAdapter"
        ),
        quote_assets=("USD", "KZT"),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(self.info.adapter_id, {})
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, int(cfg.get("timeout_seconds", 15)))
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(source_url, result)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_kase_global(
                    result.get("text") or "",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=float(cfg.get("stale_after_hours", 72.0)),
                )
                source_status = "reachable"
            except (KaseGlobalParseError, TypeError, ValueError) as exc:
                error = f"KASE Global parser failed: {exc}"[:300]
                parser_failures.append({"source": "kase_global", "source_url": source_url, "error": error})
                observations = [_failure_observation(source_url, result, error)]
                source_status = "degraded"
        freshness_states = {str(row.get("freshness_state") or "unknown") for row in observations}
        freshness_state = "fresh" if "fresh" in freshness_states else "stale" if "stale" in freshness_states else "unknown"
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1239,
                "source_status": source_status,
                "source_urls": [SOURCE_URL, MARKET_MAKER_NOTICE_URL, DIGITAL_ASSET_ETF_NOTICE_URL],
                "fetch_status": {"kase_global": _source_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": "official_daily_market_reference" if source_status == "reachable" else "unknown",
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "paper_only": True,
            },
        )


register_adapter(KazakhstanStockExchangeKaseGlobalAdapter())
