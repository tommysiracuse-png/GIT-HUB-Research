"""Official public market-data adapter for RSE "Toshkent" (UZSE).

The exchange pages expose completed trades, per-security trade history, and
corporate-action split records without an API key.  These are useful official
references, but they are not executable quotes.  Every normalized row remains
watch-only so the adapter cannot make a broker or real-money route reachable.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, utc_now
from scan_batch import ScanBatch


TRADE_RESULTS_URL = "https://uzse.uz/trade_results/?locale=en"
SECURITY_HISTORY_URL = (
    "https://www.uzse.uz/isu_info_splits/BND?isu_cd=UZ6058027AB0&locale=en"
)
SPLITS_URL = (
    "https://www.uzse.uz/isu_info_splits?isu_cd=UZ6058027AB0&market=BND"
)
NEGO_BOARD_URL = "https://uzse.uz/boards/3136?locale=en"
FOP_BOARD_URL = "https://uzse.uz/boards/3589?locale=en"
BOARD_TARIFF_ARCHIVE_URL = "https://uzse.uz/exchange/archival_rates?locale=en"

UZBEKISTAN_TIME = dt.timezone(dt.timedelta(hours=5))

BOARD_NAMES = {
    "G1": "Main Board",
    "T1": "Nego Board",
    "NC": "FoP Board",
    # The exchange's 2023 review used T2 for early FoP test transactions.
    "T2": "FoP Board",
}
BOARD_SURFACES = {
    "Main Board": "uzse_main_board_trade_results",
    "Nego Board": "uzse_nego_board_trade_results",
    "FoP Board": "uzse_fop_board_trade_results",
}
BOARD_ACTIVATION_SURFACE = "uzse_board_trade_results"
SECURITY_HISTORY_ACTIVATION_SURFACE = "uzse_security_trade_history"


class UzseParseError(ValueError):
    """Raised when a reachable UZSE source no longer matches its public schema."""


_MONTHS = {
    "jan": 1,
    "january": 1,
    "янв": 1,
    "января": 1,
    "yanvar": 1,
    "feb": 2,
    "february": 2,
    "фев": 2,
    "февраля": 2,
    "fevral": 2,
    "mar": 3,
    "march": 3,
    "мар": 3,
    "марта": 3,
    "mart": 3,
    "apr": 4,
    "april": 4,
    "апр": 4,
    "апреля": 4,
    "aprel": 4,
    "may": 5,
    "мая": 5,
    "май": 5,
    "jun": 6,
    "june": 6,
    "июн": 6,
    "июня": 6,
    "iyun": 6,
    "jul": 7,
    "july": 7,
    "июл": 7,
    "июля": 7,
    "iyul": 7,
    "aug": 8,
    "august": 8,
    "авг": 8,
    "августа": 8,
    "avgust": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "сен": 9,
    "сент": 9,
    "сентября": 9,
    "sentabr": 9,
    "oct": 10,
    "october": 10,
    "окт": 10,
    "октября": 10,
    "oktabr": 10,
    "nov": 11,
    "november": 11,
    "ноя": 11,
    "ноября": 11,
    "noyabr": 11,
    "dec": 12,
    "december": 12,
    "дек": 12,
    "декабря": 12,
    "dekabr": 12,
}


def _number(value: Any) -> float | None:
    text = re.sub(r"[^0-9,.-]", "", str(value or "").replace("\u00a0", ""))
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif text.count(",") > 1:
        text = text.replace(",", "")
    elif "," in text:
        whole, fraction = text.rsplit(",", 1)
        text = whole + fraction if len(fraction) == 3 else whole + "." + fraction
    try:
        return float(text)
    except ValueError:
        return None


def _split_ratio(value: Any) -> float | None:
    text = str(value or "").strip()
    ratio_match = re.fullmatch(r"([0-9.,]+)\s*:\s*([0-9.,]+)", text)
    if ratio_match:
        numerator = _number(ratio_match.group(1))
        denominator = _number(ratio_match.group(2))
        if numerator is not None and denominator not in (None, 0):
            return numerator / denominator
        return None
    return _number(value)


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _month(value: str) -> int | None:
    return _MONTHS.get(str(value or "").strip().lower().rstrip("."))


def _event_time(
    value: str,
    *,
    default_year: int | None = None,
    default_date: dt.date | None = None,
) -> dt.datetime | None:
    text = " ".join(str(value or "").replace("г.", " ").split())
    try:
        iso_date = dt.date.fromisoformat(text)
    except ValueError:
        iso_date = None
    if iso_date:
        return dt.datetime.combine(iso_date, dt.time.min, tzinfo=UZBEKISTAN_TIME)
    time_only = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if time_only and default_date:
        try:
            return dt.datetime.combine(
                default_date,
                dt.time(
                    int(time_only.group(1)),
                    int(time_only.group(2)),
                    int(time_only.group(3) or 0),
                ),
                tzinfo=UZBEKISTAN_TIME,
            )
        except ValueError:
            return None
    numeric = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?:[, ]+(\d{1,2}):(\d{2}))?",
        text,
    )
    if numeric:
        try:
            return dt.datetime(
                int(numeric.group(3)),
                int(numeric.group(2)),
                int(numeric.group(1)),
                int(numeric.group(4) or 0),
                int(numeric.group(5) or 0),
                tzinfo=UZBEKISTAN_TIME,
            )
        except ValueError:
            return None

    day_first = re.search(
        r"\b(\d{1,2})\s+([^\d,\s]+)[,.]?\s+(?:(\d{4})[, ]+)?(\d{1,2}):(\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if day_first:
        month = _month(day_first.group(2))
        year = int(day_first.group(3) or default_year or 0)
        if month and year:
            try:
                return dt.datetime(
                    year,
                    month,
                    int(day_first.group(1)),
                    int(day_first.group(4)),
                    int(day_first.group(5)),
                    tzinfo=UZBEKISTAN_TIME,
                )
            except ValueError:
                return None

    month_first = re.search(
        r"\b([^\d,\s]+)\s+(\d{1,2}),?\s+(\d{4})[, ]+(\d{1,2}):(\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if month_first:
        month = _month(month_first.group(1))
        if month:
            try:
                return dt.datetime(
                    int(month_first.group(3)),
                    month,
                    int(month_first.group(2)),
                    int(month_first.group(4)),
                    int(month_first.group(5)),
                    tzinfo=UZBEKISTAN_TIME,
                )
            except ValueError:
                return None
    return None


def _freshness(
    observed_at: dt.datetime,
    received_at: str | None,
    stale_after_hours: float,
) -> tuple[str, float]:
    age = max(
        0.0,
        (
            _received_time(received_at).astimezone(dt.timezone.utc)
            - observed_at.astimezone(dt.timezone.utc)
        ).total_seconds(),
    )
    state = "fresh" if age <= max(0.0, float(stale_after_hours)) * 3600.0 else "stale"
    return state, round(age, 3)


def _identity(value: str) -> tuple[str | None, str | None]:
    match = re.search(r"\b(UZ[A-Z0-9]{10})\b(?:\s+([A-Z0-9._-]+))?", str(value or ""), re.I)
    if not match:
        return None, None
    return match.group(1).upper(), (match.group(2) or match.group(1)).upper()


def _market_type(market: str) -> tuple[str, str]:
    market = str(market or "").upper()
    if market == "STK":
        return "equity", "local_equity"
    if market == "BND":
        return "bond", "local_bond"
    if market == "RPO":
        return "repo", "securities_repo"
    if market == "FCT":
        return "foreign_currency_security", "local_security"
    return "security", "local_security"


def parse_uzse_trade_results(
    document: str,
    *,
    source_url: str = TRADE_RESULTS_URL,
    received_at: str | None = None,
    stale_after_hours: float = 72.0,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Normalize completed trades across Main, Nego, and FoP board codes."""

    if not isinstance(document, str) or not document.strip():
        raise UzseParseError("trade-results response is empty")
    table = next(
        (
            rows
            for rows in html_tables(document)
            if rows
            and len(rows[0]) >= 9
            and any(token in " ".join(rows[0]).lower() for token in ("brd id", "площадка", "maydon"))
        ),
        None,
    )
    if not table:
        raise UzseParseError("trade-results table with board identifier was not found")

    observations: list[dict[str, Any]] = []
    invalid_rows = 0
    for ordinal, row in enumerate(table[1:], 1):
        if len(row) < 10:
            invalid_rows += 1
            continue
        event_time = _event_time(row[0])
        security_code, symbol = _identity(row[2])
        market = str(row[5] or "").strip().upper()
        board_id = str(row[6] or "").strip().upper()
        price = _number(row[7])
        quantity = _number(row[8])
        trading_value = _number(row[9])
        if not event_time or not security_code or not board_id or price is None or price <= 0:
            invalid_rows += 1
            continue
        board_name = BOARD_NAMES.get(board_id, f"Board {board_id}")
        market_type, asset_class = _market_type(market)
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        timestamp_key = event_time.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        inst_id = f"UZSE:{security_code}:{board_id}:TRADE:{timestamp_key}:{ordinal}"
        observations.append(
            {
                "venue": "UZSE",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "security_code": security_code,
                "symbol": symbol,
                "name": str(row[3] or symbol).strip(),
                "security_type": str(row[4] or "").strip() or None,
                "base": symbol,
                "quote": "UZS",
                "market": market,
                "board_id": board_id,
                "board_name": board_name,
                "market_type": market_type,
                "market_surface": BOARD_SURFACES.get(board_name, "uzse_other_board_trade_results"),
                "activation_market_surface": BOARD_ACTIVATION_SURFACE,
                "asset_class": asset_class,
                "trade_type": "official_completed_trade",
                "direction": "watch_only",
                "last": price,
                "trade_price": price,
                "trade_quantity": quantity,
                "local_quote_volume": trading_value,
                "trade_value_uzs": trading_value,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_completed_trade",
                "freshness_state": freshness_state,
                "freshness_basis": "official_trade_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "RSE Toshkent official trade results",
                "source_url": source_url,
                "candidate_reject_reason": "completed_trade_not_executable_quote",
            }
        )
    if not observations:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise UzseParseError(f"no usable trade-result rows{detail}")
    observations.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return observations[: max(1, int(limit))]


def _history_identity(
    document: str,
    source_url: str,
    received_at: str | None,
) -> tuple[str, str, str, dt.date]:
    tables = html_tables(document)
    summary = " ".join(cell for table in tables[:3] for row in table for cell in row)
    security_code, symbol = _identity(summary)
    query = parse_qs(urlparse(source_url).query)
    if not security_code:
        security_code, symbol = _identity(str((query.get("isu_cd") or [""])[0]))
    path_match = re.search(r"/isu_info_splits/([A-Z0-9]+)", urlparse(source_url).path, re.I)
    market = path_match.group(1).upper() if path_match else str((query.get("market") or ["SEC"])[0]).upper()
    dated = re.findall(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", summary)
    default_date = _received_time(received_at).astimezone(UZBEKISTAN_TIME).date()
    if dated:
        day, month, year = dated[-1]
        try:
            default_date = dt.date(int(year), int(month), int(day))
        except ValueError:
            pass
    if not security_code:
        raise UzseParseError("security code was not found on history page")
    return security_code, symbol or security_code, market, default_date


def parse_uzse_security_history(
    document: str,
    *,
    source_url: str = SECURITY_HISTORY_URL,
    received_at: str | None = None,
    stale_after_hours: float = 72.0,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Normalize the completed-trade history table from a security detail page."""

    if not isinstance(document, str) or not document.strip():
        raise UzseParseError("security-history response is empty")
    tables = html_tables(document)
    histories = [
        rows
        for rows in tables
        if rows
        and len(rows[0]) >= 5
        and any(
            token in str(rows[0][0]).lower()
            for token in ("time", "date", "время", "дата", "vaqt", "sana")
        )
        and any(token in str(rows[0][1]).lower() for token in ("price", "цена", "narx"))
    ]
    if not histories:
        raise UzseParseError("security-history table with Time/Date and Price columns was not found")
    security_code, symbol, market, default_date = _history_identity(
        document, source_url, received_at
    )
    market_type, asset_class = _market_type(market)

    observations: list[dict[str, Any]] = []
    invalid_rows = 0
    for history in histories:
        header = " ".join(history[0]).lower()
        adjusted_daily = "split" in header or "closed price" in header
        surface = (
            "uzse_split_adjusted_security_history"
            if adjusted_daily
            else "uzse_security_trade_history"
        )
        for ordinal, row in enumerate(history[1:], 1):
            if len(row) < 5:
                invalid_rows += 1
                continue
            event_time = _event_time(
                row[0],
                default_year=default_date.year,
                default_date=default_date,
            )
            price = _number(row[1])
            if not event_time or price is None or price <= 0:
                invalid_rows += 1
                continue
            freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
            timestamp_key = event_time.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            record_type = "ADJUSTED_HISTORY" if adjusted_daily else "HISTORY"
            inst_id = f"UZSE:{security_code}:{record_type}:{timestamp_key}:{ordinal}"
            observation = {
                "venue": "UZSE",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "security_code": security_code,
                "symbol": symbol,
                "base": symbol,
                "quote": "UZS",
                "market": market,
                "market_type": market_type,
                "market_surface": surface,
                "activation_market_surface": SECURITY_HISTORY_ACTIVATION_SURFACE,
                "asset_class": asset_class,
                "trade_type": (
                    "official_split_adjusted_history"
                    if adjusted_daily
                    else "official_security_history"
                ),
                "direction": "watch_only",
                "last": price,
                "trade_price": price,
                "price_change": _number(row[2]),
                "trade_quantity": _number(row[3]),
                "local_quote_volume": _number(row[4]),
                "trade_value_uzs": _number(row[4]),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": (
                    "official_split_adjusted_history"
                    if adjusted_daily
                    else "official_completed_trade_history"
                ),
                "freshness_state": freshness_state,
                "freshness_basis": (
                    "official_adjusted_history_date"
                    if adjusted_daily
                    else "official_security_trade_timestamp"
                ),
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "RSE Toshkent official security history",
                "source_url": source_url,
                "candidate_reject_reason": "security_history_not_executable_quote",
            }
            if adjusted_daily:
                observation.update(
                    {
                        "closed_price": price,
                        "splits_applied": row[5] if len(row) > 5 and row[5] not in {"", "-"} else None,
                    }
                )
            observations.append(observation)
    if not observations:
        detail = f"; {invalid_rows} rows were invalid" if invalid_rows else ""
        raise UzseParseError(f"no usable security-history rows{detail}")
    observations.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return observations[: max(1, int(limit))]


def parse_uzse_splits(
    payload: Any,
    *,
    source_url: str = SPLITS_URL,
    received_at: str | None = None,
    security_code: str = "UZ6058027AB0",
    market: str = "BND",
    stale_after_hours: float = 8760.0,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Normalize split-adjustment records returned by the security page endpoint."""

    if not isinstance(payload, list):
        raise UzseParseError("split-adjustment payload must be an array")
    parsed: list[dict[str, Any]] = []
    invalid_rows = 0
    for ordinal, row in enumerate(payload, 1):
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        effective = _event_time(str(row.get("split_date") or row.get("date") or ""))
        if effective is None:
            try:
                date = dt.date.fromisoformat(str(row.get("split_date") or row.get("date") or ""))
                effective = dt.datetime.combine(date, dt.time.min, tzinfo=UZBEKISTAN_TIME)
            except ValueError:
                effective = None
        ratio = _split_ratio(
            row.get("split") if row.get("split") is not None else row.get("ratio")
        )
        if effective is None or ratio is None or ratio <= 0:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(effective, received_at, stale_after_hours)
        action_id = str(row.get("id") or ordinal)
        inst_id = f"UZSE:{security_code}:SPLIT:{effective.date().isoformat()}:{action_id}"
        parsed.append(
            {
                "venue": "UZSE",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "security_code": security_code,
                "symbol": security_code,
                "base": security_code,
                "quote": "N/A",
                "market": market.upper(),
                "market_type": "corporate_action",
                "market_surface": "uzse_split_adjustments",
                "asset_class": "security_corporate_action",
                "trade_type": "official_split_adjustment",
                "direction": "watch_only",
                "last": 0.0,
                "split_ratio": ratio,
                "split_effective_date": effective.date().isoformat(),
                "corporate_action_id": action_id,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_corporate_action",
                "freshness_state": freshness_state,
                "freshness_basis": "official_split_effective_date",
                "freshness_age_seconds": freshness_age,
                "session_status": "effective",
                "observed_at": effective.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "RSE Toshkent official split adjustments",
                "source_url": source_url,
                "candidate_reject_reason": "corporate_action_reference_not_order_routable",
            }
        )
    if payload and not parsed:
        raise UzseParseError(f"no usable split-adjustment rows; {invalid_rows} rows were invalid")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def _source_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
    }


def _failure_observation(
    source_url: str,
    result: dict[str, Any],
    surface: str,
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("UZSE", source_url, evidence, surface)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_reference_parser_failure"
                if parser_error
                else "public_reference_source_unavailable"
            ),
        }
    )
    return observation


class RepublicanStockExchangeToshkentAdapter:
    info = AdapterInfo(
        adapter_id="republican_stock_exchange_toshkent_public",
        venue="UZSE",
        market_type="securities_exchange",
        source="Republican Stock Exchange Toshkent official public pages",
        capabilities=(
            "public_market_data",
            "board_trade_results",
            "completed_trades",
            "ticker_reference",
            "settlement_reference",
            "main_board",
            "nego_board",
            "fop_board",
            "security_history",
            "split_adjustments",
            "corporate_actions",
            "source_health",
        ),
        aliases=(
            "republican stock exchange toshkent",
            "rse toshkent",
            "tashkent stock exchange",
            "toshkent stock exchange",
            "uzse",
        ),
        docs_url=TRADE_RESULTS_URL,
        runtime_entrypoint=(
            "adapters.venues.republican_stock_exchange_toshkent."
            "RepublicanStockExchangeToshkentAdapter"
        ),
        quote_assets=("UZS",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(
            self.info.adapter_id, {}
        )
        timeout = int(cfg.get("timeout_seconds", 15))
        limit = max(1, int(cfg.get("max_rows_per_surface", 250)))
        stale_after_hours = float(cfg.get("stale_after_hours", 72.0))
        split_stale_after_hours = float(cfg.get("split_stale_after_hours", 8760.0))
        trade_url = str(cfg.get("trade_results_url") or TRADE_RESULTS_URL)
        history_url = str(cfg.get("security_history_url") or SECURITY_HISTORY_URL)
        splits_url = str(cfg.get("splits_url") or SPLITS_URL)
        split_security_code = str(cfg.get("split_security_code") or "UZ6058027AB0")
        split_market = str(cfg.get("split_market") or "BND")

        sources: tuple[
            tuple[str, str, str, Callable[..., list[dict[str, Any]]]], ...
        ] = (
            ("trade_results", trade_url, "uzse_board_trade_results", parse_uzse_trade_results),
            ("security_history", history_url, "uzse_security_trade_history", parse_uzse_security_history),
            ("split_adjustments", splits_url, "uzse_split_adjustments", parse_uzse_splits),
        )
        observations: list[dict[str, Any]] = []
        source_health: dict[str, dict[str, Any]] = {}
        parser_failures: list[dict[str, str]] = []
        usable_sources = 0

        for source_name, source_url, surface, parser in sources:
            result = fetch_text(source_url, timeout)
            source_health[source_name] = _source_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                if source_name == "split_adjustments":
                    rows = parser(
                        json.loads(result.get("text") or ""),
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        security_code=split_security_code,
                        market=split_market,
                        stale_after_hours=split_stale_after_hours,
                        limit=limit,
                    )
                else:
                    rows = parser(
                        result.get("text") or "",
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        stale_after_hours=stale_after_hours,
                        limit=limit,
                    )
            except (json.JSONDecodeError, UzseParseError, TypeError, ValueError) as exc:
                message = f"UZSE {source_name} parser failed: {exc}"
                parser_failures.append(
                    {"source": source_name, "source_url": source_url, "error": message[:300]}
                )
                observations.append(
                    _failure_observation(source_url, result, surface, message[:300])
                )
                continue
            observations.extend(rows)
            usable_sources += 1

        fetch_statuses = [item["fetch_status"] for item in source_health.values()]
        if usable_sources == len(sources) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in fetch_statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = {str(row.get("freshness_state")) for row in observations}
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        board_counts = {
            board_name: sum(1 for row in observations if row.get("board_name") == board_name)
            for board_name in ("Main Board", "Nego Board", "FoP Board")
        }

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "source_status": source_status,
                "source_urls": [trade_url, history_url, splits_url],
                "board_definition_urls": [
                    NEGO_BOARD_URL,
                    FOP_BOARD_URL,
                    BOARD_TARIFF_ARCHIVE_URL,
                ],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "session_state": "completed_trades_and_effective_corporate_actions",
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "board_observation_count": board_counts,
                "paper_only": True,
            },
        )


register_adapter(RepublicanStockExchangeToshkentAdapter())
