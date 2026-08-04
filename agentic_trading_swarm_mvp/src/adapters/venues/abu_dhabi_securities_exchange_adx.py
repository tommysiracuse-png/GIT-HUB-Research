"""ADX official ETF factsheet adapter for CHADX15 and UAED.

The public factsheets disclose month-end NAV and fund structure, not a live
tradable quote.  Observations therefore remain watch-only and can never enter
an order path.
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, health_observation
from scan_batch import ScanBatch


CATALOG_URL = "https://www.adx.ae/investors/Products/ETF"
CHADX15_FACTSHEET_URL = "https://apigateway.adx.ae/adx/cdn/1.0/content/download/5093497"
UAED_FACTSHEET_URL = "https://apigateway.adx.ae/adx/cdn/1.0/content/download/4803020"
NEWS_URL = "https://www.adx.ae/about-adx/media/adx-news/adx-first-us-shariah-etf-listing"
MARKET_SURFACE = "adx_official_etf_factsheet_nav"
ADX_TIME = dt.timezone(dt.timedelta(hours=4))

FACTSHEETS = (
    {
        "symbol": "CHADX15",
        "name": "Lunate FTSE ADX 15 ETF - Income",
        "source_url": CHADX15_FACTSHEET_URL,
    },
    {
        "symbol": "UAED",
        "name": "Chimera S&P UAE UCITS ETF - Income",
        "source_url": UAED_FACTSHEET_URL,
    },
)


class AdxEtfFactsheetParseError(ValueError):
    """Raised when an official ADX factsheet no longer matches its schema."""


def extract_pdf_text(body: bytes) -> str:
    """Extract visible text from a fetched ADX PDF document."""

    if not isinstance(body, bytes) or not body:
        raise AdxEtfFactsheetParseError("factsheet PDF response is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise AdxEtfFactsheetParseError(
            "pypdf is required to read the official factsheet PDF"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - upstream PDF drift must remain evidence.
        raise AdxEtfFactsheetParseError(f"factsheet PDF could not be read: {exc}") from exc
    if not text.strip():
        raise AdxEtfFactsheetParseError("factsheet PDF contains no extractable text")
    return text


def _field(text: str, label: str, pattern: str) -> str:
    match = re.search(rf"\b{label}\s+({pattern})", text, flags=re.IGNORECASE)
    if not match:
        raise AdxEtfFactsheetParseError(f"required {label} field was not found")
    return " ".join(match.group(1).split())


def _factsheet_as_of(text: str) -> dt.datetime:
    match = re.search(
        r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
        r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(20\d{2})\s+FACT\s*SHEET\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise AdxEtfFactsheetParseError("factsheet month was not found")
    try:
        month = dt.datetime.strptime(match.group(1).title(), "%B").month
        year = int(match.group(2))
        day = calendar.monthrange(year, month)[1]
        return dt.datetime(year, month, day, 15, 0, tzinfo=ADX_TIME)
    except ValueError as exc:
        raise AdxEtfFactsheetParseError("factsheet month is invalid") from exc


def _received_time(value: str | None, fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdxEtfFactsheetParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _session_status(received: dt.datetime) -> str:
    local = received.astimezone(ADX_TIME)
    if local.weekday() >= 5:
        return "closed"
    local_time = local.timetz().replace(tzinfo=None)
    return "open" if dt.time(10, 0) <= local_time < dt.time(15, 0) else "closed"


def parse_adx_etf_factsheet(
    text: str,
    *,
    expected_symbol: str,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 100.0,
) -> dict[str, Any]:
    """Normalize NAV, ISIN, and structure from one official ADX factsheet."""

    if not isinstance(text, str) or not text.strip():
        raise AdxEtfFactsheetParseError("factsheet text is empty")
    symbol = str(expected_symbol or "").strip().upper()
    if symbol not in {str(item["symbol"]) for item in FACTSHEETS}:
        raise AdxEtfFactsheetParseError("unsupported ADX ETF symbol")
    ticker_matches = {
        value.upper()
        for value in re.findall(r"(?:^|\n)\s*Ticker\s+([A-Z0-9]+)\b", text, re.IGNORECASE)
    }
    if symbol not in ticker_matches:
        raise AdxEtfFactsheetParseError(f"expected Ticker {symbol} was not found")

    isin = _field(text, "ISIN", r"[A-Z]{2}[A-Z0-9]{10}").upper()
    nav_text = _field(text, r"NAV\s*\(AED\)", r"[0-9][0-9,.]*")
    try:
        nav = float(nav_text.replace(",", ""))
    except ValueError as exc:
        raise AdxEtfFactsheetParseError("NAV (AED) is invalid") from exc
    if nav <= 0:
        raise AdxEtfFactsheetParseError("NAV (AED) must be positive")

    product_structure = _field(text, "Product Structure", r"[A-Za-z-]+")
    methodology = _field(text, "Methodology", r"[A-Za-z/-]+")
    dividend_treatment = _field(text, "Dividend Treatment", r"[A-Za-z-]+")
    domicile = _field(text, "Domicile", r"[A-Za-z]+")
    fund_type = _field(text, "Type", r"[A-Za-z]+")
    as_of = _factsheet_as_of(text)
    fetched = _received_time(received_at, as_of)
    age_seconds = max(
        0.0,
        (fetched.astimezone(dt.timezone.utc) - as_of.astimezone(dt.timezone.utc)).total_seconds(),
    )
    freshness_state = (
        "fresh" if age_seconds <= max(0.0, float(stale_after_days)) * 86400.0 else "stale"
    )
    configured = next(item for item in FACTSHEETS if item["symbol"] == symbol)
    inst_id = f"ADX:ETF:{symbol}"
    return {
        "venue": "ADX",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": str(configured["name"]),
        "base": symbol,
        "quote": "AED",
        "market_type": "exchange_traded_fund",
        "market_surface": MARKET_SURFACE,
        "asset_class": "equity_etf",
        "trade_type": "official_factsheet_nav_reference",
        "direction": "watch_only",
        "last": nav,
        "nav": nav,
        "nav_currency": "AED",
        "isin": isin,
        "product_structure": product_structure,
        "replication_methodology": methodology,
        "dividend_treatment": dividend_treatment,
        "domicile": domicile,
        "fund_type": fund_type,
        "factsheet_as_of": as_of.isoformat(),
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_month_end_nav",
        "freshness_state": freshness_state,
        "freshness_basis": "factsheet_month_end",
        "freshness_age_seconds": round(age_seconds, 3),
        "session_status": _session_status(fetched),
        "session_basis": "factsheet_disclosed_adx_trading_hours",
        "observed_at": fetched.isoformat(),
        "fetched_at": fetched.isoformat(),
        "price_source": "ADX official ETF factsheet NAV",
        "source_url": source_url,
        "source_catalog_url": CATALOG_URL,
        "candidate_reject_reason": "factsheet_nav_not_entry_quality_quote",
    }


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
    symbol: str,
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("ADX", source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "inst_id": f"ADX:ETF:{symbol}:FACTSHEET_HEALTH",
            "instrument_id": f"ADX:ETF:{symbol}:FACTSHEET_HEALTH",
            "symbol": symbol,
            "base": symbol,
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_catalog_url": CATALOG_URL,
            "candidate_reject_reason": (
                "public_factsheet_parser_failure"
                if parser_error
                else "public_factsheet_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class AbuDhabiSecuritiesExchangeAdxEtfAdapter:
    info = AdapterInfo(
        adapter_id="abu_dhabi_securities_exchange_adx_etf",
        venue="ADX",
        market_type="exchange_traded_fund",
        source="ADX official ETF factsheets",
        capabilities=(
            "public_market_data",
            "etf_factsheet",
            "net_asset_value",
            "isin",
            "product_structure",
            "fund_domicile",
            "session_reference",
            "source_health",
        ),
        aliases=(
            "abu dhabi securities exchange",
            "adx",
            "adx etf",
            "chadx15",
            "lunate ftse adx 15 etf",
            "uaed",
            "chimera s&p uae ucits etf",
        ),
        docs_url=CATALOG_URL,
        runtime_entrypoint=(
            "adapters.venues.abu_dhabi_securities_exchange_adx."
            "AbuDhabiSecuritiesExchangeAdxEtfAdapter"
        ),
        quote_assets=("AED",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 100.0)))
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_count = 0

        for factsheet in FACTSHEETS:
            symbol = str(factsheet["symbol"])
            source_url = str(factsheet["source_url"])
            result = fetch_bytes(source_url, timeout)
            fetch_status[symbol] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(symbol, source_url, result))
                continue
            try:
                # Tests and offline fixtures may supply already-extracted public text.
                text = str(result["text"]) if "text" in result else extract_pdf_text(result.get("content") or b"")
                observation = parse_adx_etf_factsheet(
                    text,
                    expected_symbol=symbol,
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                observations.append(observation)
                usable_count += 1
            except (AdxEtfFactsheetParseError, TypeError, ValueError) as exc:
                message = f"ADX {symbol} factsheet parser failed: {exc}"[:300]
                parser_failures.append(
                    {"symbol": symbol, "source_url": source_url, "error": message}
                )
                observations.append(
                    _failure_observation(symbol, source_url, result, parser_error=message)
                )

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_count == len(FACTSHEETS) and not parser_failures:
            source_status = "reachable"
        elif usable_count or parser_failures:
            source_status = "degraded"
        elif statuses and all(status == "blocked" for status in statuses):
            source_status = "blocked"
        else:
            source_status = "unavailable"

        real_rows = [row for row in observations if row.get("nav") is not None]
        freshness_states = sorted(
            {str(row.get("freshness_state") or "unknown") for row in real_rows}
        )
        session_states = sorted(
            {str(row.get("session_status") or "unknown") for row in real_rows}
        )
        freshness_state = (
            freshness_states[0]
            if len(freshness_states) == 1
            else "mixed"
            if freshness_states
            else "unknown"
        )
        session_state = (
            session_states[0]
            if len(session_states) == 1
            else "mixed"
            if session_states
            else "unknown"
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 654,
                "source_status": source_status,
                "source_urls": [
                    CATALOG_URL,
                    CHADX15_FACTSHEET_URL,
                    UAED_FACTSHEET_URL,
                    NEWS_URL,
                ],
                "fetch_status": fetch_status,
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_state,
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_entry_quality_market_price",
                "paper_only": True,
            },
        )


register_adapter(AbuDhabiSecuritiesExchangeAdxEtfAdapter())
