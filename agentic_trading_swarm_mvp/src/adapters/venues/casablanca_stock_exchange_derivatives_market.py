"""Public, paper-only Casablanca MASI 20 futures market adapter.

The Casablanca Bourse derivatives instruments page is a public reference page.
It can expose delayed prices, but it is not an order-entry or executable quote
feed.  This adapter consequently records the listed futures as watch-only
observations and retains source health and parser evidence for paper research.
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


SOURCE_URL = "https://www.casablanca-bourse.com/en/instruments-terme"
INFORMATION_DOCUMENT_URL = (
    "https://www.casablanca-bourse.com/en/insights-institutionnels/"
    "futures-masi-20-information-document-related-standard-futures-contract"
    "?csrt=2018567531853179192"
)
INSTRUCTIONS_URL = (
    "https://media.casablanca-bourse.com/sites/default/files/es-auto-upload/en/"
    "Instructions_20260616.pdf"
)
SOURCE_URLS = (SOURCE_URL, INFORMATION_DOCUMENT_URL, INSTRUCTIONS_URL)
MARKET_SURFACE = "casablanca_masi20_index_futures"
CONTRACT_SYMBOLS = ("FMASI20JUI26", "FMASI20SEP26", "FMASI20DEC26", "FMASI20MAR27")


class CasablancaDerivativesParseError(ValueError):
    """Raised when the official derivatives page no longer has MASI 20 data."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _visible_text(document: str) -> str:
    if not isinstance(document, str) or not document.strip():
        raise CasablancaDerivativesParseError("MASI 20 derivatives response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain upstream parser evidence.
        raise CasablancaDerivativesParseError(f"invalid official HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CasablancaDerivativesParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _value(row: dict[str, str], *labels: str) -> str | None:
    for label in labels:
        value = row.get(_label(label))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _table_records(document: str) -> dict[str, dict[str, str]]:
    """Read both card-like key/value tables and conventional quote grids."""

    records: dict[str, dict[str, str]] = {}
    for table in html_tables(document):
        if not table:
            continue
        # Instrument detail cards use a sequence of label/value rows.
        pair_values = {_label(row[0]): row[1] for row in table if len(row) >= 2}
        pair_symbols = [
            symbol for symbol in CONTRACT_SYMBOLS
            if any(symbol in str(value).upper() for value in pair_values.values())
        ]
        if pair_symbols:
            for symbol in pair_symbols:
                records.setdefault(symbol, {}).update(pair_values)

        # Quote tables conventionally start with headers followed by data rows.
        if len(table) < 2 or len(table[0]) < 2:
            continue
        headers = [_label(cell) for cell in table[0]]
        if not any(header in {"ticker", "symbol", "instrument", "code"} for header in headers):
            continue
        for values in table[1:]:
            row = {headers[index]: value for index, value in enumerate(values) if index < len(headers)}
            blob = " ".join(str(value).upper() for value in row.values())
            for symbol in CONTRACT_SYMBOLS:
                if re.search(rf"(?<![A-Z0-9]){symbol}(?![A-Z0-9])", blob):
                    records.setdefault(symbol, {}).update(row)
    return records


def _session_state(text: str) -> tuple[str, dt.date | None]:
    match = re.search(
        r"Session\s+(open|closed)\s+([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        state = re.search(r"Session\s+(open|closed)", text, flags=re.IGNORECASE)
        return (state.group(1).lower() if state else "unknown", None)
    try:
        return match.group(1).lower(), dt.datetime.strptime(match.group(2), "%A, %B %d, %Y").date()
    except ValueError as exc:
        raise CasablancaDerivativesParseError("official session date is invalid") from exc


def _observation(
    symbol: str,
    fields: dict[str, str],
    *,
    source_url: str,
    fetched_at: dt.datetime,
    session_status: str,
    session_date: dt.date | None,
    stale_after_hours: float,
) -> dict[str, Any]:
    price = number(_value(fields, "price", "last price", "last", "closing price"))
    previous_close = number(_value(fields, "previous closing price", "previous close"))
    last = price if price is not None else previous_close
    if last is None:
        last, price_basis = 0.0, "contract_identity_only"
    else:
        price_basis = "official_delayed_session_price" if price is not None else "previous_close"
    if session_date is None:
        freshness_state, freshness_age_seconds = "fresh", 0.0
        freshness_basis = "official_instruments_page_fetch"
    else:
        freshness_age_seconds = float(max(0, (fetched_at.date() - session_date).days) * 86400)
        freshness_state = "fresh" if freshness_age_seconds <= stale_after_hours * 3600 else "stale"
        freshness_basis = "official_exchange_session_date"
    inst_id = f"CASABLANCA_DERIVATIVES:{symbol}"
    return {
        "venue": "CASABLANCA_DERIVATIVES",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": _value(fields, "contract", "instrument", "name") or f"MASI 20 Future {symbol[-5:]}",
        "base": "MASI20",
        "quote": "MAD",
        "market_type": "futures",
        "market_surface": MARKET_SURFACE,
        "asset_class": "equity_index_futures",
        "trade_type": "official_delayed_market_reference",
        "direction": "watch_only",
        "last": last,
        "price_basis": price_basis,
        "official_price": price,
        "previous_close": previous_close,
        "change_pct": number(_value(fields, "change", "change %", "variation")),
        "volume_mad": number(_value(fields, "volume", "turnover")),
        "quantity_traded": number(_value(fields, "quantity traded", "quantity", "traded quantity")),
        "contract": _value(fields, "contract", "instrument", "name") or "MASI 20 standard futures contract",
        "underlying": "MASI20",
        "delivery_month": symbol[-5:],
        "settlement_method": _value(fields, "payment method", "settlement method") or "cash-settled reference",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_delayed_or_contract_reference",
        "freshness_state": freshness_state,
        "freshness_basis": freshness_basis,
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "session_date": session_date.isoformat() if session_date else None,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Casablanca Stock Exchange official derivatives instruments page",
        "source_url": source_url,
        "candidate_reject_reason": "official_quote_is_not_an_executable_entry_route",
    }


def parse_casablanca_derivatives_market(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
    stale_after_hours: float = 36.0,
) -> list[dict[str, Any]]:
    """Normalize listed MASI 20 futures from the official public page."""

    text = _visible_text(document)
    present = [
        symbol for symbol in CONTRACT_SYMBOLS
        if re.search(rf"(?<![A-Z0-9]){symbol}(?![A-Z0-9])", text.upper())
    ]
    if not present:
        raise CasablancaDerivativesParseError("official page contains no expected MASI 20 futures symbols")
    fields = _table_records(document)
    fetched_at = _received_at(received_at)
    session_status, session_date = _session_state(text)
    return [
        _observation(
            symbol,
            fields.get(symbol, {}),
            source_url=source_url,
            fetched_at=fetched_at,
            session_status=session_status,
            session_date=session_date,
            stale_after_hours=max(0.0, float(stale_after_hours)),
        )
        for symbol in present
    ]


# Venue and product aliases make the parser usable from narrow integrations.
parse_casablanca_masi20_futures = parse_casablanca_derivatives_market
parse_casablanca_derivatives = parse_casablanca_derivatives_market


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(result: dict[str, Any], source_url: str, parser_error: str | None = None) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("CASABLANCA_DERIVATIVES", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_casablanca_derivatives_parser_failure"
                if parser_error else "public_casablanca_derivatives_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class CasablancaStockExchangeDerivativesMarketAdapter:
    info = AdapterInfo(
        adapter_id="casablanca_stock_exchange_derivatives_market",
        venue="CASABLANCA_DERIVATIVES",
        market_type="futures",
        source="Casablanca Stock Exchange official MASI 20 derivatives instruments page",
        capabilities=(
            "public_market_data", "ticker", "delayed_quote", "contract_identity",
            "equity_index_futures", "cash_settlement_reference", "session_reference", "source_health",
        ),
        aliases=(
            "casablanca stock exchange", "casablanca bourse", "casablanca derivatives market",
            "masi 20 futures", "masi20 futures", *tuple(symbol.lower() for symbol in CONTRACT_SYMBOLS),
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.casablanca_stock_exchange_derivatives_market."
            "CasablancaStockExchangeDerivativesMarketAdapter"
        ),
        quote_assets=("MAD",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, max(1, int(cfg.get("timeout_seconds", 15))))
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status, freshness_state, session_state = str(result.get("status") or "unavailable"), "unknown", "unknown"
        else:
            try:
                observations = parse_casablanca_derivatives_market(
                    str(result.get("text") or ""), source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=max(0.0, float(cfg.get("stale_after_hours", 36.0))),
                )
                source_status = "reachable"
                freshness_state = "stale" if any(row["freshness_state"] == "stale" for row in observations) else "fresh"
                session_state = str(observations[0]["session_status"])
            except (CasablancaDerivativesParseError, TypeError, ValueError) as exc:
                message = f"Casablanca derivatives parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status, freshness_state, session_state = "degraded", "unknown", "unknown"
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 500,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": list(dict.fromkeys((source_url, *SOURCE_URLS))),
                "fetch_status": {"instruments": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "expected_contracts": list(CONTRACT_SYMBOLS),
                "observed_contracts": [row.get("symbol") for row in observations if row.get("symbol") in CONTRACT_SYMBOLS],
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_quotes_and_order_book",
                "paper_only": True,
            },
        )


register_adapter(CasablancaStockExchangeDerivativesMarketAdapter())
