"""ADX official ETF factsheet adapter for CHADX15 and UAED.

The public factsheets disclose month-end NAV and fund structure, not a live
tradable quote.  Observations therefore remain watch-only and can never enter
an order path.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import io
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation
from scan_batch import ScanBatch


CATALOG_URL = "https://www.adx.ae/investors/Products/ETF"
CHADX15_FACTSHEET_URL = "https://apigateway.adx.ae/adx/cdn/1.0/content/download/5093497"
UAED_FACTSHEET_URL = "https://apigateway.adx.ae/adx/cdn/1.0/content/download/4803020"
NEWS_URL = "https://www.adx.ae/about-adx/media/adx-news/adx-first-us-shariah-etf-listing"
MARKET_SURFACE = "adx_official_etf_factsheet_nav"
ADX_TIME = dt.timezone(dt.timedelta(hours=4))

# These official public pages disclose contract identity and clearing terms, but
# not a no-key quote or order-book endpoint.  Keep this catalog separate from
# the ETF factsheet adapter above: each is independently discoverable and has
# a distinct capability gap.
DERIVATIVES_URL = "https://www.adx.ae/en/investors/products/derivatives"
DERIVATIVES_NEWS_URL = (
    "https://www.adx.ae/about-adx/media/adx-news/"
    "adx-lists-six-new-single-stock-futures-bloomberg-collaboration"
)
DERIVATIVES_CLEARING_URL = (
    "https://www.adx.ae/post-trade-services/clearing-and-settlement/derivatives"
)
DERIVATIVES_FEE_SCHEDULE_URL = (
    "https://www.adx.ae/-/media/adx/related-documents/fees-schedule/"
    "adx-fee-schedule_public_eng_202501.pdf"
)
DERIVATIVES_MARKET_SURFACE = "adx_equity_and_index_futures_contract_catalog"
TRADINGVIEW_ADX_QUOTE_URL = "https://www.tradingview.com/symbols/ADX-{symbol}/"

SSF_UNDERLYINGS = (
    ("ADNOC_GAS", "ADNOC Gas", "energy"),
    ("ADNOC_DRILLING", "ADNOC Drilling", "energy"),
    ("ADNOC_LOGISTICS_SERVICES", "ADNOC Logistics & Services", "logistics"),
    ("PRESIGHT_AI", "Presight AI", "artificial_intelligence"),
    ("SHARJAH_ISLAMIC_BANK", "Sharjah Islamic Bank", "financial_services"),
    ("TWO_POINT_ZERO_GROUP", "Two Point Zero Group", "diversified_holdings"),
)
SSF_COMPANION_QUOTES = {
    "ADNOC_GAS": "ADNOCGAS",
    "ADNOC_DRILLING": "ADNOCDRILL",
    "ADNOC_LOGISTICS_SERVICES": "ADNOCLS",
    "PRESIGHT_AI": "PRESIGHT",
    "SHARJAH_ISLAMIC_BANK": "SIB",
    "TWO_POINT_ZERO_GROUP": "2POINTZERO",
}

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


class AdxDerivativesParseError(ValueError):
    """Raised when an official ADX derivatives page changes its public schema."""


class _AdxVisibleTextParser(HTMLParser):
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


def _adx_visible_text(document: str) -> str:
    if not isinstance(document, str) or not document.strip():
        raise AdxDerivativesParseError("official derivatives response is empty")
    parser = _AdxVisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - retain upstream parser evidence.
        raise AdxDerivativesParseError(f"invalid official HTML response: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", html.unescape(value).casefold())


def _derivatives_received_time(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdxDerivativesParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _announcement_time(text: str) -> dt.datetime:
    match = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+20\d{2})\b", text)
    if not match:
        raise AdxDerivativesParseError("six-SSF announcement date was not found")
    try:
        return dt.datetime.strptime(match.group(1), "%d %b %Y").replace(tzinfo=ADX_TIME)
    except ValueError as exc:
        raise AdxDerivativesParseError("six-SSF announcement date is invalid") from exc


def _derivative_observation(
    *,
    symbol: str,
    name: str,
    base: str,
    asset_class: str,
    contract_type: str,
    source_url: str,
    fetched_at: dt.datetime,
    freshness_state: str,
    freshness_basis: str,
    freshness_age_seconds: float | None,
    session_status: str,
    session_basis: str,
    **details: Any,
) -> dict[str, Any]:
    inst_id = f"ADX:FUTURES:{symbol}"
    return {
        "venue": "ADX",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": base,
        "quote": "AED",
        "market_type": "futures",
        "market_surface": DERIVATIVES_MARKET_SURFACE,
        "asset_class": asset_class,
        "trade_type": "official_derivatives_contract_reference",
        "direction": "watch_only",
        # A zero value is a schema-safe placeholder, not a price.  `price_basis`
        # and the reject reason keep it out of paper and live order paths.
        "last": 0.0,
        "price_available": False,
        "price_basis": "unpriced_public_contract_catalog",
        "contract_type": contract_type,
        "settlement_method": "cash_settled",
        "clearing_house": "Abu Dhabi Clear (AD Clear)",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_contract_identity_no_public_quote",
        "freshness_state": freshness_state,
        "freshness_basis": freshness_basis,
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "session_basis": session_basis,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "ADX official derivatives public reference",
        "source_url": source_url,
        "candidate_reject_reason": "official_contract_catalog_not_entry_quality_quote",
        **details,
    }


def parse_adx_derivatives_catalog(
    document: str,
    *,
    source_url: str = DERIVATIVES_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the public FADX15 contract identity and disclosed multiplier."""

    text = _adx_visible_text(document)
    compact = _compact(text)
    markers = (
        "adxderivativesmarket",
        "singlestockfutures",
        "indexfutures",
        "fadx15indexfutures",
        "settledincash",
        "adclear",
        "100sharesperssf",
    )
    missing = [marker for marker in markers if marker not in compact]
    # ADX renders the multiplication sign as either a literal × or an ASCII x
    # in different page deliveries; both normalize to the same 1 AED contract
    # multiplier disclosure.
    if not any(
        marker in compact
        for marker in ("1aedindexforfadx15", "1aedxindexforfadx15")
    ):
        missing.append("1 AED x Index for FADX15")
    if missing:
        raise AdxDerivativesParseError(
            "official derivatives catalog missing required markers: " + ", ".join(missing)
        )
    fetched_at = _derivatives_received_time(received_at)
    return [
        _derivative_observation(
            symbol="FADX15",
            name="ADX FADX 15 Index Future",
            base="FADX15",
            asset_class="equity_index_futures",
            contract_type="index_future",
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state="reference_static",
            freshness_basis="official_derivatives_catalog_fetch",
            freshness_age_seconds=0.0,
            session_status="unknown",
            session_basis="public_catalog_has_no_live_session_state",
            index_name="FADX 15",
            contract_multiplier_aed_per_index_point=1.0,
            active_ssf_contract_count=16,
            active_index_future_count=1,
        )
    ]


def parse_adx_six_ssf_announcement(
    document: str,
    *,
    source_url: str = DERIVATIVES_NEWS_URL,
    received_at: str | None = None,
    stale_after_hours: float = 24.0 * 90.0,
) -> list[dict[str, Any]]:
    """Normalize the six officially announced July 2026 SSF underlyings."""

    text = _adx_visible_text(document)
    compact = _compact(text)
    if "sixnewsinglestockfutures" not in compact or "cashsettled" not in compact:
        raise AdxDerivativesParseError("six-SSF announcement markers were not found")
    missing = [name for _symbol, name, _sector in SSF_UNDERLYINGS if _compact(name) not in compact]
    if missing:
        raise AdxDerivativesParseError(
            "six-SSF announcement is missing documented underlyings: " + ", ".join(missing)
        )

    announced_at = _announcement_time(text)
    fetched_at = _derivatives_received_time(received_at)
    age_seconds = max(
        0.0,
        (fetched_at.astimezone(dt.timezone.utc) - announced_at.astimezone(dt.timezone.utc)).total_seconds(),
    )
    freshness_state = (
        "fresh"
        if age_seconds <= max(0.0, float(stale_after_hours)) * 3600.0
        else "stale"
    )
    return [
        _derivative_observation(
            symbol=f"SSF_{symbol}",
            name=f"{name} Single Stock Future",
            base=symbol,
            asset_class="single_stock_futures",
            contract_type="single_stock_future",
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_basis="official_listing_announcement_timestamp",
            freshness_age_seconds=round(age_seconds, 3),
            session_status="listed",
            session_basis="official_listing_announcement_not_live_session",
            underlying_name=name,
            underlying_sector=sector,
            contract_size_shares=100,
            announced_at=announced_at.isoformat(),
            announcement_contract_count=6,
        )
        for symbol, name, sector in SSF_UNDERLYINGS
    ]


def parse_adx_derivatives_clearing(document: str) -> dict[str, str]:
    """Validate the public AD Clear clearing context used by catalog rows."""

    compact = _compact(_adx_visible_text(document))
    markers = ("derivativesmarket", "abudhabiclear", "centralcounterparty", "collateral")
    missing = [marker for marker in markers if marker not in compact]
    if missing:
        raise AdxDerivativesParseError(
            "official clearing page missing required markers: " + ", ".join(missing)
        )
    return {"clearing_house": "Abu Dhabi Clear (AD Clear)", "clearing_model": "central_counterparty"}


def parse_tradingview_adx_quote(
    document: str,
    *,
    symbol: str,
    source_url: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse a public TradingView ADX quote page used as a paper-only companion price."""

    text = str(document or "").strip()
    quote_symbol = str(symbol or "").strip().upper()
    if not text:
        raise AdxDerivativesParseError("TradingView ADX quote page is empty")
    if not quote_symbol:
        raise AdxDerivativesParseError("TradingView ADX quote symbol is missing")
    match = re.search(
        rf"The current price of {re.escape(quote_symbol)} is ([0-9]+(?:\.[0-9]+)?)\s*AED",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise AdxDerivativesParseError(
            f"TradingView ADX quote page missing current price for {quote_symbol}"
        )
    try:
        last = float(match.group(1))
    except ValueError as exc:
        raise AdxDerivativesParseError(
            f"TradingView ADX quote page has invalid current price for {quote_symbol}"
        ) from exc
    if last <= 0:
        raise AdxDerivativesParseError(
            f"TradingView ADX quote page current price must be positive for {quote_symbol}"
        )
    fetched_at = _derivatives_received_time(received_at)
    return {
        "last": last,
        "price_available": True,
        "price_basis": "public_companion_underlying_spot_quote",
        "quality_status": "verified_proxy",
        "proxy_quality_status": "verified_proxy",
        "proxy_symbol": f"ADX:{quote_symbol}",
        "companion_quote_symbol": quote_symbol,
        "companion_quote_url": source_url,
        "freshness_state": "fresh",
        "freshness_basis": "public_quote_page_fetch",
        "freshness_age_seconds": 0.0,
        "session_status": "unknown",
        "session_basis": "public_quote_page_has_no_session_clock",
        "price_source": "TradingView public ADX companion quote",
        "source_record_type": "tradingview_public_symbol_faq",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }


def _apply_companion_quote(
    observation: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    """Preserve ADX contract provenance while attaching a public companion quote."""

    updated = dict(observation)
    updated["last"] = float(quote["last"])
    updated["price_available"] = True
    updated["price_basis"] = str(quote["price_basis"])
    updated["quality_status"] = str(quote["quality_status"])
    updated["proxy_quality_status"] = str(quote["proxy_quality_status"])
    updated["proxy_symbol"] = str(quote["proxy_symbol"])
    updated["freshness_state"] = str(quote["freshness_state"])
    updated["freshness_basis"] = str(quote["freshness_basis"])
    updated["freshness_age_seconds"] = float(quote["freshness_age_seconds"])
    updated["session_status"] = str(quote["session_status"])
    updated["session_basis"] = str(quote["session_basis"])
    updated["price_source"] = str(quote["price_source"])
    updated["source_record_type"] = str(quote["source_record_type"])
    updated["source_contract_url"] = updated.get("source_url")
    updated["source_url"] = str(quote["companion_quote_url"])
    updated["companion_quote_symbol"] = str(quote["companion_quote_symbol"])
    updated["companion_quote_url"] = str(quote["companion_quote_url"])
    updated["observed_at"] = str(quote["observed_at"])
    updated["fetched_at"] = str(quote["fetched_at"])
    updated["candidate_reject_reason"] = "public_companion_price_requires_strategy_logic"
    return updated


def _derivatives_fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _derivatives_failure_observation(
    source_key: str,
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("ADX", source_url, evidence, DERIVATIVES_MARKET_SURFACE)
    observation.update(
        {
            "inst_id": f"ADX:DERIVATIVES:{source_key.upper()}:HEALTH",
            "instrument_id": f"ADX:DERIVATIVES:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "ADX_DERIVATIVES",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_derivatives_parser_failure"
                if parser_error
                else "public_derivatives_source_unavailable"
            ),
        }
    )
    return observation


class AbuDhabiSecuritiesExchangeAdxDerivativesAdapter:
    """Public ADX derivative contract catalog with no execution capability."""

    info = AdapterInfo(
        adapter_id="abu_dhabi_securities_exchange_adx_derivatives",
        venue="ADX",
        market_type="futures",
        source="ADX official derivatives contract catalog",
        capabilities=(
            "public_market_data",
            "contract_catalog",
            "contract_identity",
            "equity_index_futures",
            "single_stock_futures",
            "cash_settlement_reference",
            "clearing_reference",
            "source_health",
        ),
        aliases=(
            "abu dhabi securities exchange",
            "adx",
            "adx derivatives",
            "fadx15",
            "fadx 15 index futures",
            "adnoc gas futures",
            "adnoc drilling futures",
            "presight ai futures",
            "sharjah islamic bank futures",
            "two point zero group futures",
        ),
        docs_url=DERIVATIVES_URL,
        runtime_entrypoint=(
            "adapters.venues.abu_dhabi_securities_exchange_adx."
            "AbuDhabiSecuritiesExchangeAdxDerivativesAdapter"
        ),
        quote_assets=("AED",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_hours = max(0.0, float(cfg.get("stale_after_hours", 24.0 * 90.0)))
        sources = (
            ("catalog", DERIVATIVES_URL, parse_adx_derivatives_catalog),
            ("six_ssf_announcement", DERIVATIVES_NEWS_URL, parse_adx_six_ssf_announcement),
            ("clearing", DERIVATIVES_CLEARING_URL, parse_adx_derivatives_clearing),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        companion_fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        for source_key, source_url, parser in sources:
            result = fetch_text(source_url, timeout)
            fetch_status[source_key] = _derivatives_fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_derivatives_failure_observation(source_key, source_url, result))
                continue
            try:
                if source_key == "catalog":
                    parsed = parser(
                        str(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                    )
                    observations.extend(parsed)
                elif source_key == "six_ssf_announcement":
                    parsed = parser(
                        str(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        stale_after_hours=stale_after_hours,
                    )
                    observations.extend(parsed)
                else:
                    parser(str(result.get("text") or ""))
                usable_sources += 1
            except (AdxDerivativesParseError, TypeError, ValueError) as exc:
                message = f"ADX derivatives {source_key} parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source_key": source_key, "source_url": source_url, "error": message}
                )
                observations.append(
                    _derivatives_failure_observation(source_key, source_url, result, message)
                )

        companion_failures: list[dict[str, str]] = []
        enriched_observations: list[dict[str, Any]] = []
        for observation in observations:
            base = str(observation.get("base") or "")
            quote_symbol = SSF_COMPANION_QUOTES.get(base)
            if not quote_symbol:
                enriched_observations.append(observation)
                continue
            companion_url = TRADINGVIEW_ADX_QUOTE_URL.format(symbol=quote_symbol)
            result = fetch_text(companion_url, timeout)
            companion_fetch_status[base] = _derivatives_fetch_evidence(result, companion_url)
            if not result.get("ok"):
                companion_failures.append(
                    {
                        "base": base,
                        "source_url": companion_url,
                        "error": str(result.get("error") or "public companion quote unavailable")[:300],
                    }
                )
                enriched_observations.append(observation)
                continue
            try:
                quote = parse_tradingview_adx_quote(
                    str(result.get("text") or ""),
                    symbol=quote_symbol,
                    source_url=companion_url,
                    received_at=result.get("received_at"),
                )
                enriched_observations.append(_apply_companion_quote(observation, quote))
            except (AdxDerivativesParseError, TypeError, ValueError) as exc:
                companion_failures.append(
                    {
                        "base": base,
                        "source_url": companion_url,
                        "error": f"ADX derivatives companion quote parser failed: {exc}"[:300],
                    }
                )
                enriched_observations.append(observation)
        observations = enriched_observations

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(sources) and not parser_failures and not companion_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures or companion_failures:
            source_status = "degraded"
        elif statuses and all(status == "blocked" for status in statuses):
            source_status = "blocked"
        else:
            source_status = "unavailable"

        real_rows = [row for row in observations if row.get("contract_type")]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 468,
                "source_status": source_status,
                "source_urls": [
                    DERIVATIVES_URL,
                    DERIVATIVES_NEWS_URL,
                    DERIVATIVES_CLEARING_URL,
                    DERIVATIVES_FEE_SCHEDULE_URL,
                ],
                "supplemental_reference_urls": [DERIVATIVES_FEE_SCHEDULE_URL],
                "fetch_status": fetch_status,
                "companion_fetch_status": companion_fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "companion_failures": companion_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_entry_quality_quotes_and_order_book",
                "paper_only": True,
            },
        )


register_adapter(AbuDhabiSecuritiesExchangeAdxDerivativesAdapter())
