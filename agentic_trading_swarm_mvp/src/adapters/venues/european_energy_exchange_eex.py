"""EEX public German nEHS sale and auction-result adapter.

The official CSV reports are event references, not executable quotes.  They
remain watch-only even when fresh so paper mode cannot turn an auction result
into an order route.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import unicodedata
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


DATA_PAGE_URL = "https://www.eex.com/en/markets/environmentals/german-nehs/german-nehs-data"
SALES_URL = "https://public.eex-group.com/eex/nehs-reporting/nEHS_sale_Reporting.csv"
AUCTION_URL = "https://public.eex-group.com/eex/nehs-reporting/nEHS_Auction_Reporting.csv"


class EexNehsParseError(ValueError):
    """Raised when a reachable EEX report no longer matches its public schema."""


def _header_token(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lstrip("\ufeff"))
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).lower().split())


def _report_rows(text: str) -> tuple[list[str], list[list[str]]]:
    if not isinstance(text, str) or not text.strip():
        raise EexNehsParseError("empty CSV response")
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter))
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any("date" in _header_token(cell) for cell in row)
            and any("vintage" in _header_token(cell) for cell in row)
        ),
        None,
    )
    if header_index is None:
        raise EexNehsParseError("CSV header with Date and Vintage columns was not found")
    headers = [_header_token(cell) for cell in rows[header_index]]
    return headers, [row for row in rows[header_index + 1 :] if any(str(cell).strip() for cell in row)]


def _column(headers: list[str], *terms: str) -> int:
    for index, header in enumerate(headers):
        if all(term in header for term in terms):
            return index
    raise EexNehsParseError(f"required CSV column was not found: {' + '.join(terms)}")


def _optional_column(headers: list[str], *terms: str) -> int | None:
    try:
        return _column(headers, *terms)
    except EexNehsParseError:
        return None


def _cell(row: list[str], index: int | None) -> str:
    return str(row[index]).strip() if index is not None and index < len(row) else ""


def _event_time(date_value: str, time_value: str) -> dt.datetime | None:
    value = f"{str(date_value).strip()} {str(time_value).strip()}".strip()
    for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _freshness(event_time: dt.datetime, received_at: str | None, stale_after_hours: float) -> tuple[str, float]:
    age = max(0.0, (_received_time(received_at) - event_time).total_seconds())
    state = "fresh" if age <= max(0.0, float(stale_after_hours)) * 3600.0 else "stale"
    return state, round(age, 3)


def parse_eex_nehs_auction(
    text: str,
    *,
    source_url: str = AUCTION_URL,
    received_at: str | None = None,
    stale_after_hours: float = 192.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize the official bilingual nEHS auction-result CSV."""

    headers, rows = _report_rows(text)
    date_col = _column(headers, "date")
    time_col = _column(headers, "time")
    vintage_col = _column(headers, "vintage")
    price_col = _column(headers, "auction clearing price")
    allocated_col = _column(headers, "volume allocated")
    remaining_col = _optional_column(headers, "remaining auction volume")
    bids_col = _optional_column(headers, "total volume of bids")
    bidders_col = _optional_column(headers, "total number of bidders")
    successful_col = _optional_column(headers, "successful bidders")
    revenue_col = _optional_column(headers, "revenues")
    cover_col = _optional_column(headers, "cover ratio")
    actual_cover_col = next(
        (index for index, header in enumerate(headers) if "cover ratio" in header and "actual allocated" in header),
        None,
    )
    cancellation_col = _optional_column(headers, "potential cancellation")

    parsed: list[dict] = []
    invalid_rows = 0
    for row in rows:
        event_time = _event_time(_cell(row, date_col), _cell(row, time_col))
        vintage = _cell(row, vintage_col)
        price = number(_cell(row, price_col))
        allocated = number(_cell(row, allocated_col))
        if event_time is None or not vintage or price is None or price <= 0 or allocated is None:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        symbol = f"NEZ_{vintage}"
        parsed.append(
            {
                "venue": "EEX",
                "inst_id": f"EEX:{symbol}:AUCTION:{event_time.date().isoformat()}",
                "instrument_id": f"EEX:{symbol}:AUCTION:{event_time.date().isoformat()}",
                "symbol": symbol,
                "name": f"German nEHS national emissions certificate {vintage} auction",
                "base": symbol,
                "quote": "EUR_PER_NEZ",
                "market_type": "auction_reference",
                "market_surface": "eex_german_nehs_auction_results",
                "asset_class": "national_emissions_certificate",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "auction_clearing_price": price,
                "allocated_volume": allocated,
                "remaining_auction_volume": number(_cell(row, remaining_col)),
                "total_bid_volume": number(_cell(row, bids_col)),
                "bidder_count": int(number(_cell(row, bidders_col)) or 0),
                "successful_bidder_count": int(number(_cell(row, successful_col)) or 0),
                "revenue_eur": number(_cell(row, revenue_col)),
                "cover_ratio": number(_cell(row, cover_col)),
                "allocated_volume_cover_ratio": number(_cell(row, actual_cover_col)),
                "cancellation_notice": _cell(row, cancellation_col) or None,
                "vintage": int(vintage) if vintage.isdigit() else vintage,
                "event_date": event_time.date().isoformat(),
                "event_time_utc": event_time.isoformat(),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_event_result",
                "freshness_state": freshness_state,
                "freshness_basis": "official_auction_close_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "EEX German nEHS auction report",
                "source_url": source_url,
                "candidate_reject_reason": "auction_result_reference_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} data rows were invalid" if invalid_rows else ""
        raise EexNehsParseError(f"no usable nEHS auction result rows{detail}")
    parsed.sort(key=lambda item: str(item["observed_at"]), reverse=True)
    return parsed[: max(1, int(limit))]


def parse_eex_nehs_sales(
    text: str,
    *,
    source_url: str = SALES_URL,
    received_at: str | None = None,
    stale_after_hours: float = 192.0,
    limit: int = 250,
) -> list[dict]:
    """Normalize the official bilingual nEHS fixed-price sales CSV."""

    headers, rows = _report_rows(text)
    date_col = _column(headers, "date")
    time_col = _column(headers, "time")
    vintage_col = _column(headers, "vintage")
    price_col = _column(headers, "price")
    volume_col = _column(headers, "volume")
    trades_col = _optional_column(headers, "number of trades")
    buyers_col = _optional_column(headers, "number of buyers")
    revenue_col = _optional_column(headers, "certificate-revenue")
    disclaimer_col = _optional_column(headers, "disclaimer")

    parsed: list[dict] = []
    invalid_rows = 0
    for row in rows:
        event_time = _event_time(_cell(row, date_col), _cell(row, time_col))
        vintage = _cell(row, vintage_col)
        price = number(_cell(row, price_col))
        volume = number(_cell(row, volume_col))
        if event_time is None or not vintage or price is None or price <= 0 or volume is None:
            invalid_rows += 1
            continue
        freshness_state, freshness_age = _freshness(event_time, received_at, stale_after_hours)
        symbol = f"NEZ_{vintage}"
        disclaimer = _cell(row, disclaimer_col)
        parsed.append(
            {
                "venue": "EEX",
                "inst_id": f"EEX:{symbol}:SALE:{event_time.date().isoformat()}",
                "instrument_id": f"EEX:{symbol}:SALE:{event_time.date().isoformat()}",
                "symbol": symbol,
                "name": f"German nEHS national emissions certificate {vintage} sale",
                "base": symbol,
                "quote": "EUR_PER_NEZ",
                "market_type": "sale_reference",
                "market_surface": "eex_german_nehs_sales_results",
                "asset_class": "national_emissions_certificate",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": price,
                "sale_price": price,
                "sold_volume": volume,
                "transaction_count": int(number(_cell(row, trades_col)) or 0),
                "buyer_count": int(number(_cell(row, buyers_col)) or 0),
                "revenue_eur": number(_cell(row, revenue_col)),
                "vintage": int(vintage) if vintage.isdigit() else vintage,
                "event_date": event_time.date().isoformat(),
                "event_time_utc": event_time.isoformat(),
                "result_finality": "preliminary" if disclaimer else "final",
                "result_disclaimer": disclaimer or None,
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_event_result",
                "freshness_state": freshness_state,
                "freshness_basis": "official_sale_timestamp",
                "freshness_age_seconds": freshness_age,
                "session_status": "closed",
                "observed_at": event_time.isoformat(),
                "fetched_at": received_at or utc_now(),
                "price_source": "EEX German nEHS sales report",
                "source_url": source_url,
                "candidate_reject_reason": "sale_result_reference_not_executable_quote",
            }
        )
    if not parsed:
        detail = f"; {invalid_rows} data rows were invalid" if invalid_rows else ""
        raise EexNehsParseError(f"no usable nEHS sale result rows{detail}")
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
    observation = health_observation("EEX", source_url, evidence, surface)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": "public_reference_parser_failure"
            if parser_error
            else "public_reference_source_unavailable",
        }
    )
    return observation


class EexGermanNehsAdapter:
    info = AdapterInfo(
        adapter_id="eex_german_nehs_public",
        venue="EEX",
        market_type="emissions_auction",
        source="EEX official German nEHS public CSV reports",
        capabilities=(
            "auction_results",
            "sale_results",
            "settlement_reference",
            "volume",
            "participant_statistics",
            "source_health",
        ),
        aliases=(
            "european energy exchange",
            "eex",
            "german nehs",
            "nez",
            "national emissions certificates",
        ),
        docs_url=DATA_PAGE_URL,
        runtime_entrypoint="adapters.venues.european_energy_exchange_eex.EexGermanNehsAdapter",
        quote_assets=("EUR_PER_NEZ",),
        default_cache_minutes=30,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(self.info.adapter_id, {})
        timeout = int(cfg.get("timeout_seconds", 15))
        limit = max(1, int(cfg.get("max_rows_per_report", 250)))
        stale_after_hours = float(cfg.get("stale_after_hours", 192.0))
        sources = (
            ("auction", AUCTION_URL, parse_eex_nehs_auction, "eex_german_nehs_auction_results"),
            ("sales", SALES_URL, parse_eex_nehs_sales, "eex_german_nehs_sales_results"),
        )
        observations: list[dict] = []
        parser_failures: list[dict[str, str]] = []
        source_health: dict[str, dict[str, Any]] = {}
        usable_reports = 0

        for report_type, source_url, parser, surface in sources:
            result = fetch_text(source_url, timeout)
            source_health[report_type] = _source_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_url, result, surface))
                continue
            try:
                rows = parser(
                    result.get("text") or "",
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                )
            except (csv.Error, EexNehsParseError, TypeError, ValueError) as exc:
                message = f"EEX {report_type} parser failed: {exc}"
                parser_failures.append(
                    {"report_type": report_type, "source_url": source_url, "error": message[:300]}
                )
                observations.append(_failure_observation(source_url, result, surface, message[:300]))
                continue
            observations.extend(rows)
            usable_reports += 1

        fetch_statuses = [item["fetch_status"] for item in source_health.values()]
        if usable_reports == len(sources) and not parser_failures:
            source_status = "reachable"
        elif usable_reports:
            source_status = "degraded"
        elif parser_failures:
            source_status = "degraded"
        elif "blocked" in fetch_statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        freshness_states = {str(row.get("freshness_state")) for row in observations}
        freshness_state = "fresh" if "fresh" in freshness_states else "stale" if "stale" in freshness_states else "unknown"

        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "source_status": source_status,
                "source_urls": [AUCTION_URL, SALES_URL],
                "fetch_status": source_health,
                "freshness_state": freshness_state,
                "session_state": "closed_event_results",
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "auction_observation_count": sum(
                    1 for row in observations if row.get("market_surface") == "eex_german_nehs_auction_results"
                ),
                "sales_observation_count": sum(
                    1 for row in observations if row.get("market_surface") == "eex_german_nehs_sales_results"
                ),
                "paper_only": True,
            },
        )


register_adapter(EexGermanNehsAdapter())
