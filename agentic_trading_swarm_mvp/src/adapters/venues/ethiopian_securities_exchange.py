"""Official Ethiopian Securities Exchange public reference observations.

The ESX listed-companies page is public and requires no API key.  It provides
issuer identities and listing dates, not executable or entry-quality prices,
so every successful observation remains watch-only.

The fixed-income pages are similarly public and describe the instrument
catalog and trading session without publishing executable quotes.  That
surface is registered separately so the equity and debt adapter specs retain
independent runtime health, caches, and capability records.
"""

from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser
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

FIXED_INCOME_OVERVIEW_URL = "https://esx.et/fixed-income-market/"
FIXED_INCOME_INSTRUMENTS_URL = "https://esx.et/fixed-income-market/instruments/"
FIXED_INCOME_OPERATIONS_URL = (
    "https://esx.et/fixed-income-market/trading-and-operations/"
)
FIXED_INCOME_MARKET_SURFACE = "ethiopian_securities_exchange_fixed_income"

FIXED_INCOME_INSTRUMENTS = (
    {
        "symbol": "GOVT_TBILL",
        "name": "Government T-Bills",
        "instrument_type": "government_t_bill",
        "issuer_type": "government",
        "maturity_bucket": "money_market_up_to_one_year",
        "tenor_days": [28, 91, 182, 364],
        "rate_type": "fixed_discount",
        "minimum_investment_etb": 5000.0,
    },
    {
        "symbol": "COMMERCIAL_PAPER",
        "name": "Commercial Papers",
        "instrument_type": "commercial_paper",
        "issuer_type": "corporate",
        "maturity_bucket": "money_market_under_270_days",
        "maximum_maturity_days": 269,
        "rate_type": "discount",
    },
    {
        "symbol": "REPO",
        "name": "Repurchase Agreements / Repos",
        "instrument_type": "repo",
        "issuer_type": "secured_money_market_counterparty",
        "maturity_bucket": "money_market_1_to_7_days",
        "tenor_days": [1, 7],
        "rate_type": "negotiated",
    },
    {
        "symbol": "TREASURY_BOND",
        "name": "Treasury Bonds",
        "instrument_type": "treasury_bond",
        "issuer_type": "government",
        "maturity_bucket": "long_term_over_one_year",
        "rate_type": "fixed_coupon",
    },
    {
        "symbol": "CORPORATE_BOND",
        "name": "Corporate Bonds",
        "instrument_type": "corporate_bond",
        "issuer_type": "corporate",
        "maturity_bucket": "long_term_over_one_year",
        "rate_type": "fixed_or_variable_coupon",
    },
)

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


class EsxFixedIncomeParseError(ValueError):
    """Raised when a reachable ESX fixed-income page has an unknown schema."""


class _VisibleTextParser(HTMLParser):
    """Extract visible page text while excluding script and style payloads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.parts.append(data)


def _column(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _visible_text(document: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise EsxFixedIncomeParseError("fixed-income response is not valid HTML") from exc
    return " ".join(" ".join(parser.parts).split())


def _parse_date(value: Any, field: str, symbol: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise EsxEquityListingsParseError(
            f"{symbol} has invalid {field}: {value!r}"
        ) from exc


def _parse_received_at(
    value: str | None,
    error_type: type[ValueError] = EsxEquityListingsParseError,
) -> dt.datetime:
    if not value:
        value = utc_now()
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise error_type("received_at is not an ISO-8601 timestamp") from exc
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


def parse_esx_fixed_income_instruments(
    document: str,
    *,
    source_url: str = FIXED_INCOME_INSTRUMENTS_URL,
    received_at: str | None = None,
    session_status: str = "unknown",
) -> list[dict[str, Any]]:
    """Normalize the five debt instrument classes on the official ESX page."""

    if not isinstance(document, str) or not document.strip():
        raise EsxFixedIncomeParseError("fixed-income instruments response is empty")
    text = _visible_text(document)
    normalized = _column(text)
    required_terms = {
        "government t-bills": ("treasury bills", "t bills"),
        "commercial papers": ("commercial papers", "commercial paper"),
        "repos": ("repurchase agreements", "repurchase agreement", "repos"),
        "treasury bonds": ("treasury bonds", "treasury bond"),
        "corporate bonds": ("corporate bonds", "corporate bond"),
    }
    missing = [
        label
        for label, alternatives in required_terms.items()
        if not any(_column(term) in normalized for term in alternatives)
    ]
    expected_tenors = {28, 91, 182, 364}
    observed_tenors = {
        int(value)
        for value in re.findall(r"\b(28|91|182|364)\s*[- ]\s*days?\b", text, re.IGNORECASE)
    }
    if observed_tenors != expected_tenors:
        missing.append("T-Bill tenors 28/91/182/364 days")
    if not re.search(r"\bETB\s*5[\s,]?000\b", text, re.IGNORECASE):
        missing.append("T-Bill minimum investment ETB 5,000")
    if not re.search(r"\b(?:less than|under)\s+270\s+days\b", text, re.IGNORECASE):
        missing.append("commercial paper maturity under 270 days")
    if not re.search(r"\b1\s*[-–—]\s*7\s+day", text, re.IGNORECASE):
        missing.append("repo tenor 1-7 days")
    if missing:
        raise EsxFixedIncomeParseError(
            "fixed-income page is missing required instrument evidence: " + ", ".join(missing)
        )

    fetched_at = _parse_received_at(received_at, EsxFixedIncomeParseError)
    observations: list[dict[str, Any]] = []
    for item in FIXED_INCOME_INSTRUMENTS:
        instrument = dict(item)
        symbol = str(instrument.pop("symbol"))
        inst_id = f"ESX:FIXED_INCOME:{symbol}"
        observations.append(
            {
                "venue": "ESX",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": symbol,
                "base": symbol,
                "quote": "ETB",
                "market_type": "fixed_income_reference",
                "market_surface": FIXED_INCOME_MARKET_SURFACE,
                "asset_class": "fixed_income",
                "trade_type": "official_instrument_catalog",
                "direction": "watch_only",
                "last": 0.0,
                **instrument,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_instrument_classification",
                "freshness_state": "fresh",
                "freshness_basis": "successful_public_fetch",
                "freshness_age_seconds": 0.0,
                "session_status": session_status,
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Ethiopian Securities Exchange fixed-income instruments",
                "source_url": source_url,
                "candidate_reject_reason": (
                    "official_instrument_catalog_not_entry_quality_quote"
                ),
            }
        )
    return observations


def _clock_minutes(value: str) -> int:
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", value, re.IGNORECASE)
    if not match:
        raise EsxFixedIncomeParseError(f"invalid ESX trading time: {value!r}")
    hour, minute, meridiem = match.groups()
    hour_value = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
    return hour_value * 60 + int(minute)


def parse_esx_fixed_income_session(
    document: str,
    *,
    received_at: str | None = None,
) -> dict[str, Any]:
    """Parse ESX's public session table and classify the fetch time in EAT."""

    if not isinstance(document, str) or not document.strip():
        raise EsxFixedIncomeParseError("fixed-income operations response is empty")
    schedule: dict[str, tuple[int, int]] = {}
    holidays: dict[dt.date, str] = {}
    for table in html_tables(document):
        if not table:
            continue
        headers = [_column(value) for value in table[0]]
        if {"session", "time"} <= set(headers):
            session_index = headers.index("session")
            time_index = headers.index("time")
            for row in table[1:]:
                if max(session_index, time_index) >= len(row):
                    continue
                label = _column(row[session_index]).replace(" ", "_")
                times = re.findall(
                    r"\d{1,2}:\d{2}\s*[AP]M", row[time_index], re.IGNORECASE
                )
                if not times:
                    continue
                start = _clock_minutes(times[0])
                end = _clock_minutes(times[-1]) if len(times) > 1 else start
                schedule[label] = (start, end)
        elif {"date", "holiday"} <= set(headers):
            date_index = headers.index("date")
            holiday_index = headers.index("holiday")
            for row in table[1:]:
                if max(date_index, holiday_index) >= len(row):
                    continue
                match = re.search(
                    r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b", row[date_index]
                )
                if not match:
                    continue
                try:
                    holiday_date = dt.datetime.strptime(match.group(1), "%b %d, %Y").date()
                except ValueError:
                    try:
                        holiday_date = dt.datetime.strptime(match.group(1), "%B %d, %Y").date()
                    except ValueError:
                        continue
                holidays[holiday_date] = str(row[holiday_index]).strip()

    if "pre_open" not in schedule or "continuous" not in schedule or "close" not in schedule:
        raise EsxFixedIncomeParseError(
            "trading calendar table with pre-open, continuous, and close rows was not found"
        )
    fetched_at = _parse_received_at(received_at, EsxFixedIncomeParseError)
    local_time = fetched_at.astimezone(ADDIS_ABABA_TIME)
    local_minutes = local_time.hour * 60 + local_time.minute
    holiday_name = holidays.get(local_time.date())
    if holiday_name:
        status = "holiday_closed"
    elif local_time.weekday() >= 5:
        status = "weekend_closed"
    elif schedule["pre_open"][0] <= local_minutes < schedule["pre_open"][1]:
        status = "pre_open"
    elif schedule["continuous"][0] <= local_minutes < schedule["continuous"][1]:
        status = "continuous"
    else:
        status = "closed"
    return {
        "session_status": status,
        "session_state": status,
        "session_timezone": "Africa/Addis_Ababa",
        "local_observed_at": local_time.isoformat(),
        "trading_date": local_time.date().isoformat(),
        "holiday_name": holiday_name,
        "trading_hours": {
            label: {"start_minutes": value[0], "end_minutes": value[1]}
            for label, value in schedule.items()
        },
    }


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


def _fixed_income_failure_observation(
    result: dict[str, Any],
    source_url: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation(
        "ESX", source_url, evidence, FIXED_INCOME_MARKET_SURFACE
    )
    observation.update(
        {
            "inst_id": "ESX:FIXED_INCOME:ADAPTER_HEALTH",
            "instrument_id": "ESX:FIXED_INCOME:ADAPTER_HEALTH",
            "market_type": "fixed_income_reference",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_fixed_income_parser_failure"
                if parser_error
                else "public_fixed_income_source_unavailable"
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


class EthiopianSecuritiesExchangeFixedIncomeAdapter:
    info = AdapterInfo(
        adapter_id="ethiopian_securities_exchange_fixed_income",
        venue="ESX",
        market_type="fixed_income_reference",
        source="Ethiopian Securities Exchange official fixed-income market pages",
        capabilities=(
            "public_market_data",
            "catalog",
            "fixed_income_instrument_catalog",
            "government_securities",
            "commercial_paper",
            "repo",
            "corporate_bonds",
            "trading_session",
            "source_health",
        ),
        aliases=(
            "ethiopian securities exchange fixed income",
            "ethiopia securities exchange fixed income",
            "esx fixed income",
            "government t-bills",
            "commercial papers",
            "repos",
            "treasury bonds",
            "corporate bonds",
        ),
        docs_url=FIXED_INCOME_OVERVIEW_URL,
        runtime_entrypoint=(
            "adapters.venues.ethiopian_securities_exchange."
            "EthiopianSecuritiesExchangeFixedIncomeAdapter"
        ),
        quote_assets=("ETB",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        urls = {
            "overview": str(cfg.get("overview_url") or FIXED_INCOME_OVERVIEW_URL),
            "instruments": str(
                cfg.get("instruments_url")
                or cfg.get("source_url")
                or FIXED_INCOME_INSTRUMENTS_URL
            ),
            "trading_and_operations": str(
                cfg.get("operations_url") or FIXED_INCOME_OPERATIONS_URL
            ),
        }
        results = {name: fetch_text(url, timeout) for name, url in urls.items()}
        parser_failures: list[dict[str, str]] = []

        session: dict[str, Any] = {
            "session_status": "unknown",
            "session_state": "unknown",
            "session_timezone": "Africa/Addis_Ababa",
        }
        operations_result = results["trading_and_operations"]
        if operations_result.get("ok"):
            try:
                session = parse_esx_fixed_income_session(
                    str(operations_result.get("text") or ""),
                    received_at=operations_result.get("received_at"),
                )
            except (EsxFixedIncomeParseError, TypeError, ValueError) as exc:
                message = f"ESX fixed-income session parser failed: {exc}"[:300]
                parser_failures.append(
                    {
                        "source_url": urls["trading_and_operations"],
                        "parser": "trading_session",
                        "error": message,
                    }
                )

        instruments_result = results["instruments"]
        if not instruments_result.get("ok"):
            observations = [
                _fixed_income_failure_observation(
                    instruments_result, urls["instruments"]
                )
            ]
        else:
            try:
                observations = parse_esx_fixed_income_instruments(
                    str(instruments_result.get("text") or ""),
                    source_url=urls["instruments"],
                    received_at=instruments_result.get("received_at"),
                    session_status=str(session["session_status"]),
                )
            except (EsxFixedIncomeParseError, TypeError, ValueError) as exc:
                message = f"ESX fixed-income instruments parser failed: {exc}"[:300]
                parser_failures.append(
                    {
                        "source_url": urls["instruments"],
                        "parser": "fixed_income_instruments",
                        "error": message,
                    }
                )
                observations = [
                    _fixed_income_failure_observation(
                        instruments_result, urls["instruments"], message
                    )
                ]

        if not instruments_result.get("ok"):
            source_status = str(instruments_result.get("status") or "unavailable")
        elif parser_failures or any(not result.get("ok") for result in results.values()):
            source_status = "degraded"
        else:
            source_status = "reachable"
        freshness_state = (
            "fresh"
            if observations and all(row.get("data_status") == "reachable" for row in observations)
            else "unknown"
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1296,
                "source_status": source_status,
                "source_url": urls["instruments"],
                "source_urls": list(urls.values()),
                "fetch_status": {
                    name: _fetch_evidence(results[name], urls[name]) for name in urls
                },
                "freshness_state": freshness_state,
                "freshness_states": sorted(
                    {str(row.get("freshness_state") or "unknown") for row in observations}
                ),
                "session_state": session["session_state"],
                "session_states": sorted(
                    {str(row.get("session_status") or "unknown") for row in observations}
                ),
                "session": session,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_prices_yields_and_executable_quotes",
                "paper_only": True,
            },
        )


register_adapter(EthiopianSecuritiesExchangeAdapter())
register_adapter(EthiopianSecuritiesExchangeFixedIncomeAdapter())
