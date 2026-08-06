"""SGX public Bitcoin and Ethereum perpetual futures references.

SGX publishes public announcement and factsheet documents for its Bitcoin and
Ethereum perpetual futures. Those documents expose contract specifications and
recent activity summaries, but not anonymous public order entry or a live
public order book. This adapter therefore emits paper-only, watch-only
observations while preserving source health and parser failures.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


DOCS_URL = "https://www.sgx.com/derivatives/products/cryptoperps"
ANNOUNCEMENT_URL = "https://links.sgx.com/1.0.0/corporate-announcements/HDP6UNVS7KTCFTYD/"
FACTSHEET_URL = "https://api2.sgx.com/sites/default/files/2026-06/SGX%20Crypto%20Perpetual%20Futures%20Factsheet_June2026_0.pdf"
VENUE = "SINGAPORE_EXCHANGE_SGX"
MARKET_SURFACE = "sgx_crypto_perpetual_futures"
SINGAPORE = dt.timezone(dt.timedelta(hours=8), name="Asia/Singapore")
_T_SESSION_START = dt.time(7, 5)
_T_SESSION_END = dt.time(16, 0)
_T_PLUS_ONE_START = dt.time(16, 5)
_T_PLUS_ONE_END = dt.time(5, 15)
_REFERENCE_STALE_AFTER_DAYS = 45.0

_SERIES = {
    "BTP": {
        "asset_name": "Bitcoin",
        "base": "BTC",
        "contract_symbol": "BTP",
        "funding_symbol": "BTFR",
    },
    "ETP": {
        "asset_name": "Ethereum",
        "base": "ETH",
        "contract_symbol": "ETP",
        "funding_symbol": "ETFR",
    },
}


class SingaporeExchangeSgxParseError(ValueError):
    """Raised when SGX public factsheets or announcements lose required fields."""


def extract_pdf_text(body: bytes) -> str:
    """Extract searchable text from a public SGX PDF."""

    if not isinstance(body, bytes) or not body:
        raise SingaporeExchangeSgxParseError("SGX public PDF is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared dependency.
        raise SingaporeExchangeSgxParseError("pypdf is required to read SGX public PDFs") from exc
    try:
        text = "\n".join(str(page.extract_text() or "") for page in PdfReader(io.BytesIO(body)).pages)
    except Exception as exc:  # noqa: BLE001 - retain source drift as watch-only evidence.
        raise SingaporeExchangeSgxParseError(f"SGX public PDF could not be read: {exc}") from exc
    if not text.strip():
        raise SingaporeExchangeSgxParseError("SGX public PDF contains no extractable text")
    return text


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {
        **((root.get("adapters") or {}).get(adapter_id) or {}),
        **(root.get(adapter_id) or {}),
    }


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SingaporeExchangeSgxParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date(value: str, field: str) -> dt.date:
    cleaned = " ".join(str(value or "").replace("-", " ").split())
    try:
        return dt.date.fromisoformat(str(value or ""))
    except ValueError:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    raise SingaporeExchangeSgxParseError(f"invalid {field}: {value!r}")


def _format_clock(value: str) -> str:
    cleaned = " ".join(str(value or "").replace(".", ":").split()).upper()
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return dt.datetime.strptime(cleaned, fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise SingaporeExchangeSgxParseError(f"invalid SGX session time: {value!r}")


def _compact(document: str) -> str:
    return " ".join(str(document or "").split())


def _numeric(value: str) -> float:
    cleaned = re.sub(r"[^0-9.+-]", "", str(value or "").replace(",", ""))
    if not cleaned:
        raise SingaporeExchangeSgxParseError(f"numeric value was empty: {value!r}")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise SingaporeExchangeSgxParseError(f"invalid numeric value: {value!r}") from exc


def _series_last_value(document: str, label: str) -> float:
    match = re.search(rf"{re.escape(label)}\s+([0-9,\s.]+?)(?=\s+[A-Z][A-Z0-9+ ]+\(|\s+SGX Crypto Perps -)", _compact(document))
    if not match:
        raise SingaporeExchangeSgxParseError(f"factsheet series {label!r} was not found")
    values = [_numeric(token) for token in match.group(1).split()]
    if not values:
        raise SingaporeExchangeSgxParseError(f"factsheet series {label!r} had no numeric observations")
    return float(values[-1])


def _freshness(reference_date: dt.date, fetched_at: dt.datetime) -> tuple[str, float]:
    reference_at = dt.datetime.combine(reference_date, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
    age_seconds = max(0.0, (fetched_at - reference_at).total_seconds())
    state = "fresh" if age_seconds <= _REFERENCE_STALE_AFTER_DAYS * 86400.0 else "stale"
    return state, round(age_seconds, 3)


def _session_status(fetched_at: dt.datetime) -> str:
    local = fetched_at.astimezone(SINGAPORE)
    weekday = local.weekday()
    moment = local.time()
    if weekday == 6:
        return "weekend_closed"
    if weekday == 5:
        return "t_plus_1_session" if moment <= _T_PLUS_ONE_END else "weekend_closed"
    if weekday in {1, 2, 3, 4} and moment <= _T_PLUS_ONE_END:
        return "t_plus_1_session"
    if weekday <= 4 and _T_SESSION_START <= moment <= _T_SESSION_END:
        return "t_session"
    if weekday <= 3 and moment >= _T_PLUS_ONE_START:
        return "t_plus_1_session"
    if weekday == 4 and moment >= _T_PLUS_ONE_START:
        return "t_plus_1_session"
    return "daily_break"


def parse_sgx_announcement(
    document: str,
    *,
    source_url: str = ANNOUNCEMENT_URL,
) -> dict[str, Any]:
    """Extract launch timing metadata from the official SGX announcement page."""

    if not isinstance(document, str) or not document.strip():
        raise SingaporeExchangeSgxParseError("announcement response is empty")
    stripped = html.unescape(re.sub(r"<[^>]+>", "\n", document))
    normalized = "\n".join(line.strip() for line in stripped.splitlines() if line.strip())
    if "Bitcoin and Ethereum perpetual futures" not in normalized:
        raise SingaporeExchangeSgxParseError("announcement did not mention Bitcoin and Ethereum perpetual futures")
    broadcast = re.search(r"Date &Time of Broadcast\s*\n([0-9]{2}-[A-Za-z]{3}-[0-9]{4} [0-9:]{8})", normalized)
    launch = re.search(r"Launching on ([0-9]{1,2} [A-Za-z]+ [0-9]{4})", normalized)
    if not broadcast or not launch:
        raise SingaporeExchangeSgxParseError("announcement launch markers were not found")
    broadcast_at = dt.datetime.strptime(broadcast.group(1), "%d-%b-%Y %H:%M:%S").replace(tzinfo=SINGAPORE)
    launch_date = _date(launch.group(1), "launch date")
    return {
        "source_url": source_url,
        "broadcast_at": broadcast_at.isoformat(),
        "launch_date": launch_date.isoformat(),
    }


def _extract_contract_section(document: str, asset_name: str) -> str:
    compact = _compact(document)
    heading = f"Contract Specifications: SGX {asset_name} Perpetual Futures"
    pattern = rf"{re.escape(heading)}(.*?)(?=Contract Specifications: SGX [A-Za-z]+ Perpetual Futures|“iEdge”|Disclaimer:|$)"
    match = re.search(pattern, compact, re.S)
    if not match:
        raise SingaporeExchangeSgxParseError(f"{asset_name} contract specification section was not found")
    return match.group(1)


def _underlying_index(section: str) -> str:
    match = re.search(r"Underlying Index (.*?) Product Code", section, re.S)
    if not match:
        raise SingaporeExchangeSgxParseError("underlying index field was not found")
    return " ".join(match.group(1).split())


def _contract_terms(section: str) -> dict[str, Any]:
    contract = re.search(
        r"Contract size ([0-9.]+) (Bitcoin|Ethereum),.*?~US\$([0-9,]+(?:\.[0-9]+)?) \(based on index value as of ([^)]+)\)",
        section,
        re.S,
    )
    if not contract:
        raise SingaporeExchangeSgxParseError("contract size and reference notional were not found")
    perp_tick = re.search(
        r"PERP\s+([0-9.]+) index points? \(US\$ ([0-9.]+)\)\s+TAS\s+([0-9.]+) index point[s]? \(US\$ ([0-9.]+)\)",
        section,
        re.S,
    )
    if not perp_tick:
        raise SingaporeExchangeSgxParseError("perpetual tick-size schedule was not found")
    funding = re.search(r"published approximately every ([0-9]+) minutes", section, re.I)
    if not funding:
        raise SingaporeExchangeSgxParseError("funding publication cadence was not found")
    contract_code = re.search(r"SGX ([A-Z]{3}) ([A-Z]{5})", section)
    if not contract_code:
        raise SingaporeExchangeSgxParseError("SGX perpetual contract code was not found")
    funding_code = re.search(r"SGX ([A-Z]{4})", section[contract_code.end() :])
    if not funding_code:
        raise SingaporeExchangeSgxParseError("SGX funding-rate code was not found")
    trading_hours = re.search(
        r"T Session .*?Opening : ([0-9.]+ am) - ([0-9.]+ pm).*?T\+1 Session Opening : ([0-9.]+ pm) - ([0-9.]+ am)",
        section,
        re.S | re.I,
    )
    if not trading_hours:
        raise SingaporeExchangeSgxParseError("SGX trading hours were not found")
    contract_size = _numeric(contract.group(1))
    reference_notional = _numeric(contract.group(3))
    if contract_size is None or contract_size <= 0 or reference_notional is None or reference_notional <= 0:
        raise SingaporeExchangeSgxParseError("contract size or reference notional was not positive")
    reference_date = _date(contract.group(4), "reference price date")
    return {
        "contract_size": float(contract_size),
        "reference_notional_usd": float(reference_notional),
        "reference_price_date": reference_date.isoformat(),
        "reference_index_price_usd": round(float(reference_notional) / float(contract_size), 8),
        "perp_tick_index_points": _numeric(perp_tick.group(1)),
        "perp_tick_value_usd": _numeric(perp_tick.group(2)),
        "tas_tick_index_points": _numeric(perp_tick.group(3)),
        "tas_tick_value_usd": _numeric(perp_tick.group(4)),
        "funding_publication_interval_minutes": int(funding.group(1)),
        "contract_code": contract_code.group(1),
        "tas_code": contract_code.group(2),
        "funding_rate_code": funding_code.group(1),
        "t_session_start_sgt": _format_clock(trading_hours.group(1)),
        "t_session_end_sgt": _format_clock(trading_hours.group(2)),
        "t_plus_one_start_sgt": _format_clock(trading_hours.group(3)),
        "t_plus_one_end_sgt": _format_clock(trading_hours.group(4)),
    }


def parse_sgx_crypto_perpetual_factsheet(
    document: str,
    *,
    source_url: str = FACTSHEET_URL,
    received_at: str | None = None,
    announcement: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize public SGX Bitcoin and Ethereum perpetual contract references."""

    if not isinstance(document, str) or not document.strip():
        raise SingaporeExchangeSgxParseError("factsheet response is empty")
    if "Contract Specifications: SGX Bitcoin Perpetual Futures" not in document or "Contract Specifications: SGX Ethereum Perpetual Futures" not in document:
        raise SingaporeExchangeSgxParseError("factsheet did not contain both Bitcoin and Ethereum contract sections")
    fetched_at = _received_time(received_at)
    btp_dav = _series_last_value(document, "BTP DAV (lots)")
    etp_dav = _series_last_value(document, "ETP DAV (lots)")
    btp_oi = _series_last_value(document, "BTP OI (lots)")
    etp_oi = _series_last_value(document, "ETP OI (lots)")
    activity_map = {"BTP": {"avg_daily_volume_lots": btp_dav, "open_interest_lots": btp_oi}, "ETP": {"avg_daily_volume_lots": etp_dav, "open_interest_lots": etp_oi}}
    rows: list[dict[str, Any]] = []
    for symbol, spec in _SERIES.items():
        section = _extract_contract_section(document, spec["asset_name"])
        terms = _contract_terms(section)
        freshness_state, freshness_age_seconds = _freshness(_date(terms["reference_price_date"], "reference price date"), fetched_at)
        rows.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:{symbol}",
                "instrument_id": f"{VENUE}:{symbol}",
                "symbol": symbol,
                "name": f"SGX {spec['asset_name']} Perpetual Futures",
                "base": spec["base"],
                "quote": "USD",
                "market_type": "perp",
                "market_surface": MARKET_SURFACE,
                "asset_class": "crypto_derivatives",
                "trade_type": "institutional_perp_contract_reference",
                "direction": "watch_only",
                "last": terms["reference_index_price_usd"],
                "price_available": True,
                "reference_index_price_usd": terms["reference_index_price_usd"],
                "reference_contract_notional_usd": terms["reference_notional_usd"],
                "reference_price_as_of": terms["reference_price_date"],
                "contract_size_base_units": terms["contract_size"],
                "underlying_index": _underlying_index(section),
                "sgx_contract_code": terms["contract_code"],
                "sgx_tas_code": terms["tas_code"],
                "sgx_funding_rate_code": terms["funding_rate_code"],
                "perp_tick_index_points": terms["perp_tick_index_points"],
                "perp_tick_value_usd": terms["perp_tick_value_usd"],
                "tas_tick_index_points": terms["tas_tick_index_points"],
                "tas_tick_value_usd": terms["tas_tick_value_usd"],
                "avg_daily_volume_lots": activity_map[symbol]["avg_daily_volume_lots"],
                "open_interest_lots": activity_map[symbol]["open_interest_lots"],
                "activity_reference_month": terms["reference_price_date"][:7],
                "funding_publication_interval_minutes": terms["funding_publication_interval_minutes"],
                "supports_weekend_trading": False,
                "settlement_type": "cash_settled_usd",
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_contract_spec_reference",
                "freshness_state": freshness_state,
                "freshness_basis": "factsheet_reference_price_date",
                "freshness_age_seconds": freshness_age_seconds,
                "session_status": _session_status(fetched_at),
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "SGX crypto perpetual futures factsheet",
                "source_url": source_url,
                "docs_url": DOCS_URL,
                "launch_date": announcement.get("launch_date") if isinstance(announcement, dict) else None,
                "announcement_broadcast_at": announcement.get("broadcast_at") if isinstance(announcement, dict) else None,
                "paper_route": "synthetic_reference",
                "route_required": True,
                "execution_mode": "paper_only",
                "paper_experiment_eligible": False,
                "candidate_reject_reason": "public_sgx_contract_reference_route_needed",
            }
        )
    return rows


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
    source_key: str,
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "base": "SGX_SOURCE_HEALTH",
            "quote": "N/A",
            "market_type": "market_data_health",
            "trade_type": "official_market_reference",
            "source_key": source_key,
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": "public_sgx_parser_failure" if parser_error else "public_sgx_source_unavailable",
        }
    )
    return row


def _summarize_states(rows: list[dict[str, Any]], field: str) -> tuple[str, list[str]]:
    values = sorted({str(row.get(field) or "unknown") for row in rows}) if rows else ["unknown"]
    return (values[0], values) if len(values) == 1 else ("mixed", values)


def _source_status(real_count: int, fetch_status: dict[str, dict[str, Any]], parser_failures: list[dict[str, str]]) -> str:
    statuses = [str(item.get("fetch_status") or "unknown") for item in fetch_status.values()]
    non_reachable = [status for status in statuses if status != "reachable"]
    if parser_failures:
        return "degraded"
    if non_reachable and real_count:
        return "degraded"
    if real_count and not non_reachable:
        return "reachable"
    if not non_reachable:
        return "unavailable"
    unique = sorted(set(non_reachable))
    return unique[0] if len(unique) == 1 else "degraded"


class SingaporeExchangeSgxCryptoPerpetualAdapter:
    info = AdapterInfo(
        adapter_id="singapore_exchange_sgx_crypto_perpetual_futures",
        venue=VENUE,
        market_type="perp_contract_reference",
        source="SGX public crypto perpetual futures announcement and factsheet",
        capabilities=(
            "public_market_data",
            "event_price_reference",
            "settlement_reference",
            "contract_catalog",
            "funding_reference",
            "open_interest_reference",
            "volume_reference",
            "source_health",
        ),
        aliases=(
            "singapore exchange",
            "sgx",
            "sgx bitcoin perpetual futures",
            "sgx ethereum perpetual futures",
            "sgx crypto perpetual futures",
        ),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.singapore_exchange_sgx.SingaporeExchangeSgxCryptoPerpetualAdapter",
        quote_assets=("USD",),
        default_cache_minutes=180,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 20)))
        docs_url = str(cfg.get("docs_url") or DOCS_URL)
        announcement_url = str(cfg.get("announcement_url") or ANNOUNCEMENT_URL)
        factsheet_url = str(cfg.get("factsheet_url") or FACTSHEET_URL)

        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        announcement: dict[str, Any] | None = None

        docs = fetch_text(docs_url, timeout)
        fetch_status["docs_page"] = _fetch_evidence(docs, docs_url)
        if not docs.get("ok"):
            observations.append(_failure_observation("docs_page", docs_url, docs))

        announcement_result = fetch_text(announcement_url, timeout)
        fetch_status["announcement"] = _fetch_evidence(announcement_result, announcement_url)
        if not announcement_result.get("ok"):
            observations.append(_failure_observation("announcement", announcement_url, announcement_result))
        else:
            try:
                announcement = parse_sgx_announcement(str(announcement_result.get("text") or ""), source_url=announcement_url)
            except (SingaporeExchangeSgxParseError, TypeError, ValueError) as exc:
                message = f"SGX announcement parser failed: {exc}"[:300]
                parser_failures.append({"source_url": announcement_url, "error": message})
                observations.append(_failure_observation("announcement", announcement_url, announcement_result, message))

        factsheet_result = fetch_bytes(factsheet_url, timeout, max_bytes=6_000_000)
        fetch_status["factsheet"] = _fetch_evidence(factsheet_result, factsheet_url)
        if not factsheet_result.get("ok"):
            observations.append(_failure_observation("factsheet", factsheet_url, factsheet_result))
        else:
            try:
                document = str(factsheet_result.get("text") or "") or extract_pdf_text(factsheet_result.get("content") or b"")
                observations.extend(
                    parse_sgx_crypto_perpetual_factsheet(
                        document,
                        source_url=factsheet_url,
                        received_at=factsheet_result.get("received_at"),
                        announcement=announcement,
                    )
                )
            except (SingaporeExchangeSgxParseError, TypeError, ValueError) as exc:
                message = f"SGX factsheet parser failed: {exc}"[:300]
                parser_failures.append({"source_url": factsheet_url, "error": message})
                observations.append(_failure_observation("factsheet", factsheet_url, factsheet_result, message))

        real_rows = [row for row in observations if row.get("quality_status") == "official_contract_spec_reference"]
        freshness_state, freshness_states = _summarize_states(real_rows, "freshness_state")
        session_state, session_states = _summarize_states(real_rows, "session_status")
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 834,
                "source_status": _source_status(len(real_rows), fetch_status, parser_failures),
                "fetch_status": fetch_status,
                "source_url": factsheet_url,
                "source_urls": [docs_url, announcement_url, factsheet_url],
                "docs_url": docs_url,
                "announcement_url": announcement_url,
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_state,
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_no_key_contract_reference_only_route_needed",
                "paper_only": True,
            },
        )


SingaporeExchangeSgxAdapter = SingaporeExchangeSgxCryptoPerpetualAdapter
register_adapter(SingaporeExchangeSgxCryptoPerpetualAdapter())
