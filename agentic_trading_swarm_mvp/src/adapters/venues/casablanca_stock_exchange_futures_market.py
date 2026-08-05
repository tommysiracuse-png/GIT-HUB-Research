"""Casablanca Stock Exchange official MASI 20 futures adapter.

The exchange's public instrument page exposes delayed session prices and the
contract terms without an API key.  Prices can be theoretical when no trade
has occurred, so every observation is intentionally watch-only and cannot
become an executable route.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = (
    "https://www.casablanca-bourse.com/en/live-market/"
    "instruments-derives/FMASI20SEP26"
)
MARKET_URL = "https://www.casablanca-bourse.com/en/live-market/marche-derives"
RULES_URL = (
    "https://futures.casablanca-bourse.com/themes/custom/"
    "marche_a_terme/CCP-IN-002.pdf"
)
MARKET_SURFACE = "casablanca_masi20_index_futures"


class CasablancaFuturesParseError(ValueError):
    """Raised when a reachable official contract page changes schema."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _visible_text(document: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - preserve malformed upstream evidence.
        raise CasablancaFuturesParseError(f"invalid HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_time(value: str | None) -> dt.datetime:
    raw = value or utc_now()
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CasablancaFuturesParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _first(rows: list[list[str]], label: str) -> str | None:
    wanted = label.casefold()
    for row in rows:
        if len(row) >= 2 and str(row[0]).strip().casefold() == wanted:
            return str(row[1]).strip()
    return None


def _required(rows: list[list[str]], label: str) -> str:
    value = _first(rows, label)
    if not value or value in {"-", "–", "—"}:
        raise CasablancaFuturesParseError(f"instrument field {label!r} was not found")
    return value


def _date(value: str, field: str) -> dt.date:
    try:
        return dt.datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError as exc:
        raise CasablancaFuturesParseError(f"{field} is invalid: {value!r}") from exc


def _session_state(text: str) -> tuple[str, dt.date | None]:
    match = re.search(
        r"Session\s+(open|closed)\s+"
        r"([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        state = re.search(r"Session\s+(open|closed)", text, flags=re.IGNORECASE)
        return (state.group(1).lower() if state else "unknown", None)
    try:
        session_date = dt.datetime.strptime(match.group(2), "%A, %B %d, %Y").date()
    except ValueError as exc:
        raise CasablancaFuturesParseError("official session date is invalid") from exc
    return match.group(1).lower(), session_date


def parse_casablanca_masi20_future(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_hours: float = 36.0,
) -> list[dict[str, Any]]:
    """Normalize one official MASI 20 futures instrument page."""

    if not isinstance(document, str) or not document.strip():
        raise CasablancaFuturesParseError("MASI 20 futures response is empty")
    text = _visible_text(document)
    tables = html_tables(document)
    instrument = next(
        (table for table in tables if _first(table, "Ticker")),
        None,
    )
    session = next(
        (table for table in tables if _first(table, "Price") is not None),
        None,
    )
    if instrument is None:
        raise CasablancaFuturesParseError("official instrument information table was not found")
    if session is None:
        raise CasablancaFuturesParseError("official session data table was not found")

    ticker = _required(instrument, "Ticker").upper()
    if not re.fullmatch(r"FMASI20(?:MAR|JUI|SEP|DEC)\d{2}", ticker):
        raise CasablancaFuturesParseError(f"unexpected MASI 20 futures ticker: {ticker!r}")
    contract = _required(instrument, "Contract")
    underlying = _required(instrument, "Underlying Asset").upper()
    if "MASI20" not in re.sub(r"\s+", "", contract.upper()) or underlying != "MASI20":
        raise CasablancaFuturesParseError("page is not a MASI 20 futures contract")

    isin = _required(instrument, "ISIN").upper()
    if not re.fullmatch(r"MA[A-Z0-9]{10}", isin):
        raise CasablancaFuturesParseError(f"unexpected Moroccan ISIN: {isin!r}")
    last_trading_date = _date(_required(instrument, "Last Trading Day"), "last trading day")
    trading_unit = number(_required(instrument, "Trading Unit (MAD)"))
    initial_deposit = number(_required(instrument, "Initial Deposit"))
    if trading_unit is None or trading_unit <= 0:
        raise CasablancaFuturesParseError("trading unit must be positive")
    if initial_deposit is None or initial_deposit <= 0:
        raise CasablancaFuturesParseError("initial deposit must be positive")

    official_price = number(_first(session, "Price"))
    previous_close = number(_first(session, "Previous closing price"))
    last = official_price if official_price is not None else previous_close
    price_basis = "official_session_price" if official_price is not None else "previous_close"
    if last is None:
        last = 0.0
        price_basis = "contract_identity_only"

    fetched_at = _received_time(received_at)
    session_status, session_date = _session_state(text)
    if session_date is None:
        freshness_state = "unknown"
        freshness_age_seconds = None
    else:
        age_days = max(0, (fetched_at.date() - session_date).days)
        freshness_age_seconds = float(age_days * 86400)
        freshness_state = (
            "fresh"
            if freshness_age_seconds <= max(0.0, float(stale_after_hours)) * 3600.0
            else "stale"
        )

    inst_id = f"CASABLANCA_FUTURES:{ticker}"
    return [
        {
            "venue": "CASABLANCA_FUTURES",
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": ticker,
            "name": f"{contract} {ticker[-5:]}",
            "base": "MASI20",
            "quote": "MAD",
            "market_type": "futures",
            "market_surface": MARKET_SURFACE,
            "asset_class": "equity_index_futures",
            "trade_type": "official_delayed_market_reference",
            "direction": "watch_only",
            "last": last,
            "price_basis": price_basis,
            "official_price": official_price,
            "previous_close": previous_close,
            "change_pct": number(_first(session, "Change")),
            "open": number(_first(session, "Opening")),
            "low": number(_first(session, "Low")),
            "high": number(_first(session, "High")),
            "volume_mad": number(_first(session, "Volume")),
            "quantity_traded": number(_first(session, "Quantity traded")),
            "transaction_count": number(_first(session, "Number of transactions")),
            "contract": contract,
            "isin": isin,
            "underlying": underlying,
            "underlying_type": _required(instrument, "Type of Underlying Asset"),
            "delivery_month": _required(instrument, "Maturity"),
            "last_trading_date": last_trading_date.isoformat(),
            "settlement_method": _required(instrument, "Payment Method"),
            "contract_multiplier_mad_per_index_point": trading_unit,
            "initial_deposit_mad": initial_deposit,
            "price_delay_minutes": 15,
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_delayed_or_theoretical_quote",
            "freshness_state": freshness_state,
            "freshness_basis": "official_exchange_session_date",
            "freshness_age_seconds": freshness_age_seconds,
            "session_status": session_status,
            "session_date": session_date.isoformat() if session_date else None,
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "price_source": "Casablanca Stock Exchange official futures instrument page",
            "source_url": source_url,
            "candidate_reject_reason": "official_price_is_delayed_and_may_be_theoretical",
        }
    ]


# Compatibility names for callers that identify the parser by venue or surface.
parse_casablanca_futures = parse_casablanca_masi20_future
parse_masi20_future = parse_casablanca_masi20_future


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
    observation = health_observation(
        "CASABLANCA_FUTURES", source_url, evidence, MARKET_SURFACE
    )
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_futures_parser_failure"
                if parser_error
                else "public_futures_source_unavailable"
            ),
        }
    )
    return observation


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class CasablancaStockExchangeFuturesMarketAdapter:
    info = AdapterInfo(
        adapter_id="casablanca_stock_exchange_futures_market",
        venue="CASABLANCA_FUTURES",
        market_type="futures",
        source="Casablanca Stock Exchange official MASI 20 futures page",
        capabilities=(
            "public_market_data",
            "ticker",
            "delayed_quote",
            "contract_identity",
            "equity_index_futures",
            "margin_reference",
            "session_reference",
            "source_health",
        ),
        aliases=(
            "casablanca stock exchange",
            "casablanca bourse",
            "casablanca futures market",
            "masi 20 futures",
            "masi20 future",
            "fmasi20jui26",
            "fmasi20sep26",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.casablanca_stock_exchange_futures_market."
            "CasablancaStockExchangeFuturesMarketAdapter"
        ),
        quote_assets=("MAD",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_hours = max(0.0, float(cfg.get("stale_after_hours", 36.0)))
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
            freshness_state = "unknown"
            session_state = "unknown"
        else:
            try:
                observations = parse_casablanca_masi20_future(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                )
                source_status = "reachable"
                freshness_state = str(observations[0]["freshness_state"])
                session_state = str(observations[0]["session_status"])
            except (CasablancaFuturesParseError, TypeError, ValueError) as exc:
                message = f"Casablanca MASI 20 futures parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"
                freshness_state = "unknown"
                session_state = "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 956,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": list(dict.fromkeys([source_url, SOURCE_URL, MARKET_URL, RULES_URL])),
                "fetch_status": {"instrument": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_quotes_and_order_book",
                "paper_only": True,
            },
        )


register_adapter(CasablancaStockExchangeFuturesMarketAdapter())
