"""Bank of Canada regular Treasury-bill auction reference adapter.

The Valet group is a public, no-key source linked from the Bank's regular
Treasury-bill page.  Auction results are event references rather than
executable quotes, so every row remains watch-only.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = (
    "https://www.bankofcanada.ca/markets/government-securities-auctions/"
    "calls-for-tenders-and-results/regular-treasury-bills/"
)
API_URL = "https://www.bankofcanada.ca/valet/observations/group/AUC_TBILL/json"
VALET_DOCS_URL = "https://www.bankofcanada.ca/valet-api-how-to/"
MARKET_SURFACE = "canada_regular_treasury_bill_auctions"

try:
    EASTERN_TIME = ZoneInfo("America/Toronto")
except ZoneInfoNotFoundError:  # pragma: no cover - tzdata is present in supported runtimes.
    EASTERN_TIME = dt.timezone(dt.timedelta(hours=-5))


class BankOfCanadaTreasuryBillParseError(ValueError):
    """Raised when the reachable Valet response no longer has usable rows."""


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BankOfCanadaTreasuryBillParseError(
            "received_at is not an ISO-8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _value(row: dict[str, Any], key: str) -> Any:
    cell = row.get(key)
    return cell.get("v") if isinstance(cell, dict) else cell


def _iso_date(value: Any, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise BankOfCanadaTreasuryBillParseError(f"invalid {field}") from exc


def _auction_time(auction_date: dt.date, deadline: Any) -> dt.datetime:
    raw = str(deadline or "").strip()
    try:
        auction_clock = dt.time.fromisoformat(raw)
    except ValueError:
        auction_clock = dt.time(10, 30)
    return dt.datetime.combine(auction_date, auction_clock, tzinfo=EASTERN_TIME)


def _session_status(
    *,
    official_status: str,
    fetched_at: dt.datetime,
    auction_at: dt.datetime,
    has_results: bool,
) -> str:
    if has_results or "result" in official_status.lower():
        return "results_published"
    local_fetched = fetched_at.astimezone(EASTERN_TIME)
    if local_fetched.date() < auction_at.date():
        return "auction_scheduled"
    if local_fetched <= auction_at:
        return "tender_open"
    return "results_pending"


def parse_bank_of_canada_treasury_bill_auctions(
    payload: str | dict[str, Any],
    *,
    source_url: str = API_URL,
    received_at: str | None = None,
    stale_after_hours: float = 336.0,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Normalize recent regular Treasury-bill calls and auction results."""

    if isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BankOfCanadaTreasuryBillParseError(
                f"invalid Valet JSON response: {exc.msg}"
            ) from exc
    elif isinstance(payload, dict):
        document = payload
    else:
        raise BankOfCanadaTreasuryBillParseError("Valet response must be a JSON object")

    raw_rows = document.get("observations")
    if not isinstance(raw_rows, list):
        raise BankOfCanadaTreasuryBillParseError(
            "Valet response is missing the observations array"
        )

    fetched_at = _received_time(received_at)
    parsed: list[dict[str, Any]] = []
    invalid_detail_rows = 0
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        isin = str(_value(raw, "AUC_TBILL_ISIN") or "").strip().upper()
        if not isin:
            # Valet also supplies auction-level overview rows without instruments.
            continue
        if not re.fullmatch(r"CA[A-Z0-9]{10}", isin):
            invalid_detail_rows += 1
            continue
        try:
            auction_date = _iso_date(
                _value(raw, "AUC_TBILL_AUCTION_DATE"), "auction date"
            )
            maturity_date = _iso_date(
                _value(raw, "AUC_TBILL_MATURITY_DATE"), "maturity date"
            )
        except BankOfCanadaTreasuryBillParseError:
            invalid_detail_rows += 1
            continue

        auction_at = _auction_time(
            auction_date, _value(raw, "AUC_TBILL_BID_DEADLINE")
        )
        amount = number(_value(raw, "AUC_TBILL_AMOUNT"))
        term_days = number(_value(raw, "AUC_TBILL_TERM_DAYS"))
        if amount is None or amount <= 0 or term_days is None or term_days <= 0:
            invalid_detail_rows += 1
            continue

        average_price = number(_value(raw, "AUC_TBILL_AVG_PRICE"))
        average_yield = number(_value(raw, "AUC_TBILL_AVG_YIELD"))
        high_yield = number(_value(raw, "AUC_TBILL_HIGH_YIELD"))
        official_status = str(_value(raw, "AUC_TBILL_STATUS") or "").strip()
        has_results = all(
            value is not None for value in (average_price, average_yield, high_yield)
        )
        if "result" in official_status.lower() and not has_results:
            invalid_detail_rows += 1
            continue
        age_seconds = max(
            0.0,
            (fetched_at - auction_at.astimezone(dt.timezone.utc)).total_seconds(),
        )
        freshness_state = (
            "fresh"
            if age_seconds <= max(0.0, float(stale_after_hours)) * 3600.0
            else "stale"
        )
        session_status = _session_status(
            official_status=official_status,
            fetched_at=fetched_at,
            auction_at=auction_at,
            has_results=has_results,
        )
        auction_key = str(
            _value(raw, "AUC_TBILL_KEY") or raw.get("tbill_id") or ""
        ).strip()
        issue_date_raw = _value(raw, "AUC_TBILL_ISSUE_DATE")
        issue_date = str(issue_date_raw).strip() if issue_date_raw else None
        inst_id = f"BANK_OF_CANADA:{isin}:AUCTION:{auction_date.isoformat()}"

        parsed.append(
            {
                "venue": "BANK_OF_CANADA",
                "inst_id": inst_id,
                "instrument_id": inst_id,
                "symbol": isin,
                "name": f"Government of Canada regular Treasury bill {isin}",
                "base": isin,
                "quote": "CAD_PER_100_FACE",
                "market_type": "treasury_bill_auction_reference",
                "market_surface": MARKET_SURFACE,
                "asset_class": "sovereign_treasury_bill",
                "trade_type": "official_primary_auction_result"
                if has_results
                else "official_call_for_tender",
                "direction": "watch_only",
                "last": average_price if average_price is not None else 0.0,
                "isin": isin,
                "auction_key": auction_key or None,
                "auction_date": auction_date.isoformat(),
                "auction_at": auction_at.isoformat(),
                "bid_deadline": str(
                    _value(raw, "AUC_TBILL_BID_DEADLINE") or ""
                ).strip()
                or None,
                "issue_date": issue_date,
                "maturity_date": maturity_date.isoformat(),
                "term_days": int(term_days),
                "official_status": official_status or None,
                "auction_amount_millions_cad": amount,
                "awarded_amount_millions_cad": amount if has_results else None,
                "average_price_per_100": average_price,
                "average_yield_pct": average_yield,
                "low_yield_pct": number(_value(raw, "AUC_TBILL_LOW_YIELD")),
                "high_yield_pct": high_yield,
                "stop_out_yield_pct": high_yield,
                "coverage_ratio": number(_value(raw, "AUC_TBILL_COVERAGE")),
                "tail_bps": number(_value(raw, "AUC_TBILL_TAIL")),
                "allotment_ratio_pct": number(
                    _value(raw, "AUC_TBILL_ALLOTMENT_RATIO")
                ),
                "bank_of_canada_purchase_millions_cad": number(
                    _value(raw, "AUC_TBILL_BOC_PURCHASE")
                ),
                "bank_of_canada_minimum_purchase_millions_cad": number(
                    _value(raw, "AUC_TBILL_BOC_MIN_PURCHASE")
                ),
                "total_submitted_millions_cad": number(
                    _value(raw, "AUC_TBILL_TOTAL_SUBMITTED")
                ),
                "non_competitive_submitted_millions_cad": number(
                    _value(raw, "AUC_TBILL_NON_COMPETE_AMOUNT")
                ),
                "outstanding_prior_millions_cad": number(
                    _value(raw, "AUC_TBILL_OUTSTANDING_PRIOR")
                ),
                "outstanding_after_millions_cad": number(
                    _value(raw, "AUC_TBILL_OUTSTANDING_AFTER")
                ),
                "allotted_to_distributors_pct": number(
                    _value(raw, "AUC_TBILL_PERCENT_ALLOTTED_TO_DISTRIBUTORS")
                ),
                "allotted_to_customers_pct": number(
                    _value(raw, "AUC_TBILL_PERCENT_ALLOTTED_TO_CUSTOMERS")
                ),
                "allotted_to_canadian_accounts_pct": number(
                    _value(raw, "AUC_TBILL_PERCENT_ALLOTTED_TO_CANADIAN")
                ),
                "allotted_to_foreign_accounts_pct": number(
                    _value(raw, "AUC_TBILL_PERCENT_ALLOTTED_TO_FOREIGN")
                ),
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_auction_result"
                if has_results
                else "official_call_for_tender",
                "freshness_state": freshness_state,
                "freshness_basis": "official_auction_deadline",
                "freshness_age_seconds": round(age_seconds, 3),
                "session_status": session_status,
                "observed_at": fetched_at.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Bank of Canada Valet AUC_TBILL",
                "source_url": source_url,
                "source_page_url": SOURCE_URL,
                "candidate_reject_reason": (
                    "official_auction_result_not_executable_quote"
                    if has_results
                    else "official_call_for_tender_not_executable_quote"
                ),
            }
        )

    if not parsed:
        detail = (
            f"; {invalid_detail_rows} instrument rows were invalid"
            if invalid_detail_rows
            else ""
        )
        raise BankOfCanadaTreasuryBillParseError(
            "no usable regular Treasury-bill instrument rows" + detail
        )
    parsed.sort(
        key=lambda row: (str(row["auction_at"]), str(row["isin"])), reverse=True
    )
    return parsed[: max(1, int(limit))]


# Compatibility aliases for callers that use the institution or surface name.
parse_bank_of_canada_regular_treasury_bills = (
    parse_bank_of_canada_treasury_bill_auctions
)
parse_boc_tbill_auctions = parse_bank_of_canada_treasury_bill_auctions


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
    row = health_observation(
        "BANK_OF_CANADA", source_url, evidence, MARKET_SURFACE
    )
    row.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure"
            if parser_error
            else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_page_url": SOURCE_URL,
            "candidate_reject_reason": (
                "public_treasury_bill_parser_failure"
                if parser_error
                else "public_treasury_bill_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


class BankOfCanadaRegularTreasuryBillsAdapter:
    info = AdapterInfo(
        adapter_id="bank_of_canada_regular_treasury_bills",
        venue="BANK_OF_CANADA",
        market_type="treasury_bill_auction_reference",
        source="Bank of Canada regular Treasury-bill auctions and results",
        capabilities=(
            "public_market_data",
            "auction_schedule",
            "auction_results",
            "auction_price",
            "auction_yield",
            "stop_out_yield",
            "award_size",
            "allotment_ratio",
            "coverage_ratio",
            "source_health",
        ),
        aliases=(
            "bank of canada",
            "boc",
            "government of canada treasury bills",
            "canada treasury bill auctions",
            "regular treasury bills",
            "auc_tbill",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.bank_of_canada."
            "BankOfCanadaRegularTreasuryBillsAdapter"
        ),
        quote_assets=("CAD_PER_100_FACE",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        limit = max(1, min(int(cfg.get("max_rows", 12)), 100))
        stale_after_hours = max(
            0.0, float(cfg.get("stale_after_hours", 336.0))
        )
        source_url = str(cfg.get("source_url") or API_URL)
        result = fetch_text(source_url, timeout)
        parser_failures: list[dict[str, str]] = []

        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_bank_of_canada_treasury_bill_auctions(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=stale_after_hours,
                    limit=limit,
                )
                source_status = "reachable"
            except (BankOfCanadaTreasuryBillParseError, TypeError, ValueError) as exc:
                message = f"Bank of Canada Treasury-bill parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"

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
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1061,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url, SOURCE_URL, VALET_DOCS_URL],
                "fetch_status": {"regular_treasury_bills": _fetch_evidence(result, source_url)},
                "freshness_state": freshness_state,
                "freshness_states": freshness_states,
                "session_state": session_states[0]
                if len(session_states) == 1
                else "mixed",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "entry_quality_secondary_market_bill_and_repo_quotes",
                "paper_only": True,
            },
        )


BankOfCanadaAdapter = BankOfCanadaRegularTreasuryBillsAdapter


register_adapter(BankOfCanadaRegularTreasuryBillsAdapter())
