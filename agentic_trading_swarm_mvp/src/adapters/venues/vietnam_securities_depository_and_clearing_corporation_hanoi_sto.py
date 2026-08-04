"""Public VSDC/HNX domestic-carbon settlement reference adapter.

The cited pages describe the operating and settlement surface, but do not
publish entry-quality prices.  Successful records are therefore useful market
structure observations while remaining strictly watch-only.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser
from typing import Any, Callable

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


GUIDELINE_URL = "https://vsd.vn/en/ad/195618"
SETTLEMENT_URL = "https://vsd.vn/en/sd/XAz40d2Q-9j569TvBgLQaQ"
HNX_COORDINATION_URL = (
    "https://www.hnx.vn/vi-vn/m-tin-tuc-hnx/"
    "Signing%20ceremony%20of%20Memorandum%20on%20Coordination%20on%20"
    "domestic%20carbon%20exchange%20operations-60022914-0.html"
)
SOURCE_URL = GUIDELINE_URL
MARKET_SURFACE = "vietnam_domestic_carbon_exchange_settlement"
VIETNAM_TIME = dt.timezone(dt.timedelta(hours=7))


class VietnamCarbonSettlementParseError(ValueError):
    """Raised when a reachable official page no longer has required facts."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
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
        raise VietnamCarbonSettlementParseError("official page response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
    except Exception as exc:  # noqa: BLE001 - convert HTML failures to parser evidence.
        raise VietnamCarbonSettlementParseError(f"invalid official page HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).replace("\xa0", " ").split())


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VietnamCarbonSettlementParseError(
            "received_at is not an ISO-8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date(text: str, patterns: tuple[str, ...], field: str) -> dt.date:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        values = tuple(int(value) for value in match.groups())
        if len(values) == 2:
            day, year = values
            month = 6
        else:
            day, month, year = values
        try:
            return dt.date(year, month, day)
        except ValueError as exc:
            raise VietnamCarbonSettlementParseError(
                f"official page has invalid {field} date"
            ) from exc
    raise VietnamCarbonSettlementParseError(f"official page is missing {field} date")


def _freshness(
    event_date: dt.date, received_at: str | None, stale_after_days: float
) -> tuple[dt.datetime, str, float]:
    fetched_at = _received_at(received_at)
    local_date = fetched_at.astimezone(VIETNAM_TIME).date()
    age_seconds = max(0.0, float((local_date - event_date).days * 86400))
    state = "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400.0 else "stale"
    return fetched_at, state, age_seconds


def _require(text: str, checks: dict[str, str]) -> None:
    missing = [label for label, pattern in checks.items() if not re.search(pattern, text, re.I)]
    if missing:
        raise VietnamCarbonSettlementParseError(
            "official page is missing required settlement markers: " + ", ".join(missing)
        )


def _base_observation(
    *,
    inst_id: str,
    symbol: str,
    name: str,
    base: str,
    asset_class: str,
    source_url: str,
    fetched_at: dt.datetime,
    event_date: dt.date,
    freshness_state: str,
    freshness_age_seconds: float,
    session_status: str,
    trade_type: str,
    reject_reason: str,
) -> dict[str, Any]:
    return {
        "venue": "VSDC_HNX",
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": base,
        "quote": "VND",
        "market_type": "carbon_settlement_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": asset_class,
        "trade_type": trade_type,
        "direction": "watch_only",
        "last": 0.0,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_market_structure_reference",
        "freshness_state": freshness_state,
        "freshness_basis": "official_event_date",
        "freshness_age_seconds": round(freshness_age_seconds, 3),
        "session_status": session_status,
        "event_date": event_date.isoformat(),
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "VSDC/HNX official public reference",
        "source_url": source_url,
        "candidate_reject_reason": reject_reason,
    }


def parse_vsdc_carbon_settlement_rules(
    document: str,
    *,
    source_url: str = SETTLEMENT_URL,
    received_at: str | None = None,
    stale_after_days: float = 365.0,
) -> list[dict[str, Any]]:
    """Normalize VSDC's public T+0 rules for both domestic carbon products."""

    text = _visible_text(document)
    _require(
        text,
        {
            "emission allowance or quota": r"greenhouse gas emission (?:allowance|quota)",
            "carbon credit": r"carbon credit",
            "instant per-transaction payment": r"instant payment.{0,100}per transaction",
            "T+0 settlement": r"T\s*\+\s*0",
            "simultaneous transfer and payment": r"simultaneous(?:ly)? with payment",
            "BIDV settlement bank": r"\bBIDV\b",
            "Decision 17/QD-HDTV": r"17\s*/\s*QD-HDTV",
        },
    )
    rule_date = _date(
        text,
        (
            r"Decision\s+(?:No\.\s*)?17\s*/\s*QD-HDTV\s+dated\s+(\d{1,2})/(\d{1,2})/(\d{4})",
            r"17\s*/\s*QD-HDTV.{0,80}?(\d{1,2})/(\d{1,2})/(\d{4})",
        ),
        "Decision 17/QD-HDTV",
    )
    fetched_at, freshness_state, age_seconds = _freshness(
        rule_date, received_at, stale_after_days
    )
    products = (
        (
            "VSDC_HNX:VN_GHG_EMISSION_ALLOWANCE:SETTLEMENT",
            "VN_GHG_EMISSION_ALLOWANCE",
            "Vietnam greenhouse gas emission allowance settlement",
            "greenhouse_gas_emission_allowance",
        ),
        (
            "VSDC_HNX:VN_CARBON_CREDIT:SETTLEMENT",
            "VN_CARBON_CREDIT",
            "Vietnam carbon credit settlement",
            "carbon_credit",
        ),
    )
    rows: list[dict[str, Any]] = []
    for inst_id, symbol, name, asset_class in products:
        row = _base_observation(
            inst_id=inst_id,
            symbol=symbol,
            name=name,
            base=symbol,
            asset_class=asset_class,
            source_url=source_url,
            fetched_at=fetched_at,
            event_date=rule_date,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="t_plus_zero_settlement_supported",
            trade_type="official_settlement_rule",
            reject_reason="official_settlement_rule_not_entry_quality_quote",
        )
        row.update(
            {
                "settlement_cycle": "T+0",
                "settlement_method": "instant_per_transaction",
                "delivery_principle": "simultaneous_asset_and_payment_transfer",
                "delivery_versus_payment": True,
                "settlement_bank": "BIDV",
                "depository_and_settlement_operator": "VSDC",
                "trading_venue": "HNX",
                "regulation": "Decision 17/QD-HDTV",
                "regulation_date": rule_date.isoformat(),
            }
        )
        rows.append(row)
    return rows


# Friendly aliases for callers that describe the complete venue pair/surface.
parse_vsdc_hnx_carbon_settlement = parse_vsdc_carbon_settlement_rules
parse_vietnam_carbon_settlement_surface = parse_vsdc_carbon_settlement_rules


def parse_vsdc_carbon_guideline(
    document: str,
    *,
    source_url: str = GUIDELINE_URL,
    received_at: str | None = None,
    stale_after_days: float = 365.0,
) -> list[dict[str, Any]]:
    """Normalize the official publication event for VSDC Decision 17."""

    text = _visible_text(document)
    _require(
        text,
        {
            "VSDC guideline": r"guideline.{0,100}(?:depository|settlement)",
            "emission allowance or quota": r"greenhouse gas emission (?:allowance|quota)",
            "carbon credit": r"carbon credit",
            "Decision 17/QD-HDTV": r"17\s*/\s*QD-HDTV",
        },
    )
    decision_date = _date(
        text,
        (
            r"(?:On\s+)?(\d{1,2})/(\d{1,2})/(\d{4}).{0,300}?Decision\s+(?:No\.\s*)?17",
            r"Decision\s+(?:No\.\s*)?17.{0,80}?(\d{1,2})/(\d{1,2})/(\d{4})",
        ),
        "guideline decision",
    )
    fetched_at, state, age_seconds = _freshness(
        decision_date, received_at, stale_after_days
    )
    row = _base_observation(
        inst_id="VSDC_HNX:CARBON_SETTLEMENT:GUIDELINE_17",
        symbol="CARBON_SETTLEMENT_GUIDELINE_17",
        name="VSDC domestic carbon depository and settlement guideline",
        base="VN_CARBON_MARKET",
        asset_class="carbon_market_rule",
        source_url=source_url,
        fetched_at=fetched_at,
        event_date=decision_date,
        freshness_state=state,
        freshness_age_seconds=age_seconds,
        session_status="settlement_guideline_issued",
        trade_type="official_regulatory_event",
        reject_reason="official_regulatory_event_not_entry_quality_quote",
    )
    row.update({"regulation": "Decision 17/QD-HDTV", "regulation_date": decision_date.isoformat()})
    return [row]


def parse_hnx_carbon_coordination(
    document: str,
    *,
    source_url: str = HNX_COORDINATION_URL,
    received_at: str | None = None,
    stale_after_days: float = 365.0,
) -> list[dict[str, Any]]:
    """Normalize HNX's domestic-carbon operating-coordination event."""

    text = _visible_text(document)
    _require(
        text,
        {
            "domestic carbon exchange": r"domestic carbon exchange",
            "memorandum on coordination": r"(?:memorandum|MoU).{0,80}coordination|coordination.{0,80}(?:memorandum|MoU)",
            "Hanoi Stock Exchange": r"Hanoi Stock Exchange|\bHNX\b",
            "VSDC": r"Vietnam (?:Securities|Stock) (?:Depository|Exchange)|\bVSDC\b",
        },
    )
    event_date = _date(
        text,
        (
            r"(?:On\s+)?June\s+(\d{1,2}),\s*(\d{4})",
            r"(?:On\s+)?(\d{1,2})/(\d{1,2})/(\d{4})",
        ),
        "coordination event",
    )
    # The English month form yields (day, year), unlike the numeric form.
    month_match = re.search(r"(?:On\s+)?June\s+(\d{1,2}),\s*(\d{4})", text, re.I)
    if month_match:
        event_date = dt.date(int(month_match.group(2)), 6, int(month_match.group(1)))
    fetched_at, state, age_seconds = _freshness(event_date, received_at, stale_after_days)
    row = _base_observation(
        inst_id="VSDC_HNX:DOMESTIC_CARBON_EXCHANGE:COORDINATION",
        symbol="VN_CARBON_EXCHANGE_COORDINATION",
        name="Vietnam domestic carbon exchange operating coordination",
        base="VN_CARBON_MARKET",
        asset_class="carbon_market_operations",
        source_url=source_url,
        fetched_at=fetched_at,
        event_date=event_date,
        freshness_state=state,
        freshness_age_seconds=age_seconds,
        session_status="operations_coordination_signed",
        trade_type="official_market_operation_event",
        reject_reason="official_operation_event_not_entry_quality_quote",
    )
    row.update({"trading_venue": "HNX", "depository_and_settlement_operator": "VSDC"})
    return [row]


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
    result: dict[str, Any], source_url: str, label: str, parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation("VSDC_HNX", source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"VSDC_HNX:ADAPTER_HEALTH:{label.upper()}",
            "instrument_id": f"VSDC_HNX:ADAPTER_HEALTH:{label.upper()}",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_carbon_settlement_parser_failure"
                if parser_error
                else "public_carbon_settlement_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter:
    info = AdapterInfo(
        adapter_id="vietnam_securities_depository_and_clearing_corporation_hanoi_sto",
        venue="VSDC_HNX",
        market_type="carbon_settlement_reference",
        source="VSDC and HNX official domestic carbon exchange references",
        capabilities=(
            "public_market_data",
            "carbon_allowance",
            "carbon_credit",
            "depository_reference",
            "settlement_cycle",
            "settlement_bank",
            "market_operations_reference",
            "source_health",
        ),
        aliases=(
            "vietnam securities depository and clearing corporation",
            "hanoi stock exchange",
            "vsdc",
            "hnx",
            "vietnam domestic carbon exchange",
            "greenhouse gas emission allowance",
            "carbon credit",
        ),
        docs_url=GUIDELINE_URL,
        runtime_entrypoint=(
            "adapters.venues."
            "vietnam_securities_depository_and_clearing_corporation_hanoi_sto."
            "VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter"
        ),
        quote_assets=("VND",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 365.0)))
        urls = {
            "settlement": str(cfg.get("settlement_url") or cfg.get("source_url") or SETTLEMENT_URL),
            "guideline": str(cfg.get("guideline_url") or GUIDELINE_URL),
            "coordination": str(cfg.get("coordination_url") or HNX_COORDINATION_URL),
        }
        parsers: dict[str, Callable[..., list[dict[str, Any]]]] = {
            "settlement": parse_vsdc_carbon_settlement_rules,
            "guideline": parse_vsdc_carbon_guideline,
            "coordination": parse_hnx_carbon_coordination,
        }
        results: dict[str, dict[str, Any]] = {}
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        usable_sources = 0

        for label, source_url in urls.items():
            result = fetch_text(source_url, timeout)
            results[label] = result
            if not result.get("ok"):
                observations.append(_failure_observation(result, source_url, label))
                continue
            try:
                rows = parsers[label](
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_days=stale_after_days,
                )
                observations.extend(rows)
                usable_sources += 1
            except (VietnamCarbonSettlementParseError, TypeError, ValueError) as exc:
                message = f"{label} parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations.append(_failure_observation(result, source_url, label, message))

        if usable_sources == len(urls) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        else:
            statuses = {str(result.get("status") or "unavailable") for result in results.values()}
            source_status = "blocked" if "blocked" in statuses else "unavailable"

        freshness_states = sorted(
            {str(row.get("freshness_state") or "unknown") for row in observations}
        )
        session_states = sorted(
            {str(row.get("session_status") or "unknown") for row in observations}
        )
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        session_state = session_states[0] if len(session_states) == 1 else "mixed"
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 955,
                "source_status": source_status,
                "source_url": urls["settlement"],
                "source_urls": list(urls.values()),
                "fetch_status": {
                    label: _fetch_evidence(results[label], urls[label]) for label in urls
                },
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_state,
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_entry_quality_carbon_prices",
                "paper_only": True,
            },
        )


# Retain a class name matching the generated/truncated module stem.
VietnamSecuritiesDepositoryAndClearingCorporationHanoiStoAdapter = (
    VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter
)


register_adapter(VietnamSecuritiesDepositoryAndClearingCorporationHanoiStockExchangeAdapter())
