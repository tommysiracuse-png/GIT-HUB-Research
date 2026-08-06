"""Central Bank of Bahrain Government Treasury-bill auction adapter.

The CBB's public press releases publish weekly bill allotment results.  These
are primary-auction references, not executable secondary-market prices, so the
adapter intentionally emits watch-only paper-research observations.
"""

from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://www.cbb.gov.bh/govt_securities/104888/"
RESULTS_URL = "https://www.cbb.gov.bh/media-center/cbb-treasury-bills-oversubscribed-237/"
TWELVE_MONTH_RESULTS_URL = (
    "https://www.cbb.gov.bh/media-center/"
    "cbb-12-month-treasury-bills-issue-no-126-oversubscribed/"
)
DEVELOPMENT_BOND_RESULTS_URL = (
    "https://www.cbb.gov.bh/media-center/"
    "cbb-government-development-bond-issue-no-44-oversubscribed/"
)
MARKET_SURFACE = "bahrain_government_treasury_bill_auctions"
VENUE = "CENTRAL_BANK_OF_BAHRAIN"


class CentralBankOfBahrainTreasuryBillParseError(ValueError):
    """Raised when a reachable CBB release no longer supplies auction fields."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _plain_text(payload: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(payload)
    except ValueError as exc:
        raise CentralBankOfBahrainTreasuryBillParseError("invalid HTML response") from exc
    return parser.text()


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CentralBankOfBahrainTreasuryBillParseError(
            "received_at is not an ISO-8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _match(text: str, pattern: str, field: str) -> str:
    found = re.search(pattern, text, flags=re.IGNORECASE)
    if not found:
        raise CentralBankOfBahrainTreasuryBillParseError(f"missing {field}")
    return " ".join(found.group(1).split())


def _date(value: str, field: str) -> dt.date:
    # CBB uses ``<sup>th</sup>`` ordinals, which the HTML text extractor
    # deliberately separates into e.g. ``17 th December``.
    normalized = re.sub(r"(\d)\s+(?:st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
    normalized = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip().replace(" ,", ",")
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d %B, %Y"):
        try:
            return dt.datetime.strptime(normalized, fmt).date()
        except ValueError:
            pass
    raise CentralBankOfBahrainTreasuryBillParseError(f"invalid {field}: {value!r}")


def _published_date(text: str, fallback: dt.date) -> dt.date:
    found = re.search(r"Published\s+on\s+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})", text, re.I)
    return _date(found.group(1), "publication date") if found else fallback


def parse_central_bank_of_bahrain_treasury_bill_auction(
    payload: str,
    *,
    source_url: str = RESULTS_URL,
    received_at: str | None = None,
    stale_after_hours: float = 168.0,
) -> list[dict[str, Any]]:
    """Parse an official CBB Treasury-bill allotment press release."""

    if not isinstance(payload, str) or not payload.strip():
        raise CentralBankOfBahrainTreasuryBillParseError("response must be non-empty HTML text")
    text = _plain_text(payload)
    if "Treasury Bills" not in text or "lowest accepted price" not in text.lower():
        raise CentralBankOfBahrainTreasuryBillParseError(
            "response does not contain CBB Treasury-bill auction result markers"
        )

    fetched_at = _received_time(received_at)
    issue_number = _match(text, r"issue\s+No\.?\s*(\d+)", "issue number")
    isin = _match(text, r"\(\s*ISIN\s+([A-Z0-9]{12})\s*\)", "ISIN").upper()
    amount_millions = number(
        _match(
            text,
            r"(?:week(?:ly|['’]s)|month(?:ly|['’]s))\s+BD\s+([\d,.]+)\s+million\s+issue",
            "allotted amount",
        )
    )
    maturity_days = number(_match(text, r"maturity\s+of\s+(\d+)\s+days", "maturity"))
    issue_date = _date(
        _match(text, r"issue\s+date\s+of\s+the\s+bills\s+is\s+(.+?)(?:,|\s+and\s+the\s+maturity)", "issue date"),
        "issue date",
    )
    maturity_date = _date(
        _match(text, r"maturity\s+date\s+is\s+(.+?)(?:\.|\s+The\s+weighted)", "maturity date"),
        "maturity date",
    )
    average_rate = number(
        _match(text, r"weighted\s+average\s+rate\s+of\s+interest\s+is\s+([\d.]+)\s*%", "average rate")
    )
    prior_average_rate = number(
        _match(
            text,
            r"weighted\s+average\s+rate\s+of\s+interest\s+is\s+[\d.]+\s*%\s+compared\s+to\s+([\d.]+)",
            "previous average rate",
        )
    )
    average_price = number(
        _match(text, r"approximate\s+average\s+price\s+for\s+the\s+issue\s+was\s+([\d.]+)\s*%", "average price")
    )
    lowest_accepted_price = number(
        _match(text, r"lowest\s+accepted\s+price\s+being\s+([\d.]+)\s*%", "lowest accepted price")
    )
    oversubscription_pct = number(
        _match(text, r"(?:over\s*subscribed|oversubscribed|fully\s+subscribed)\s+by\s+([\d.]+)\s*%", "subscription ratio")
    )
    if None in (amount_millions, maturity_days, average_rate, average_price, lowest_accepted_price, oversubscription_pct):
        raise CentralBankOfBahrainTreasuryBillParseError("auction result contains invalid numeric fields")

    published_at = _published_date(text, issue_date)
    result_at = dt.datetime.combine(published_at, dt.time.min, tzinfo=dt.timezone.utc)
    age_seconds = max(0.0, (fetched_at - result_at).total_seconds())
    freshness_state = "fresh" if age_seconds <= max(0.0, stale_after_hours) * 3600 else "stale"
    inst_id = f"{VENUE}:TBILL:ISSUE:{issue_number}"
    coverage_ratio = 1.0 + (float(oversubscription_pct) / 100.0)
    return [
        {
            "venue": VENUE,
            "inst_id": inst_id,
            "instrument_id": inst_id,
            "symbol": isin,
            "name": f"Bahrain Government Treasury Bill issue {issue_number}",
            "base": isin,
            "quote": "BHD_PER_100_FACE",
            "market_type": "treasury_bill_auction_reference",
            "market_surface": MARKET_SURFACE,
            "asset_class": "sovereign_treasury_bill",
            "trade_type": "official_primary_auction_result",
            "direction": "watch_only",
            "last": average_price,
            "issue_number": int(issue_number),
            "isin": isin,
            "auction_amount_millions_bhd": amount_millions,
            "awarded_amount_millions_bhd": amount_millions,
            "maturity_days": int(maturity_days),
            "term_days": int(maturity_days),
            "issue_date": issue_date.isoformat(),
            "maturity_date": maturity_date.isoformat(),
            "average_interest_rate_pct": average_rate,
            "average_yield_pct": average_rate,
            "previous_average_interest_rate_pct": prior_average_rate,
            "average_price_per_100": average_price,
            "lowest_accepted_price_per_100": lowest_accepted_price,
            "oversubscription_pct": oversubscription_pct,
            "coverage_ratio": round(coverage_ratio, 6),
            "data_status": "reachable",
            "fetch_status": "reachable",
            "quality_status": "official_auction_result",
            "freshness_state": freshness_state,
            "freshness_basis": "official_results_publication_date",
            "freshness_age_seconds": round(age_seconds, 3),
            "session_status": "results_published",
            "auction_at": result_at.isoformat(),
            "observed_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "result_published_date": published_at.isoformat(),
            "price_source": "Central Bank of Bahrain Treasury-bill allotment press release",
            "source_url": source_url,
            "source_page_url": SOURCE_URL,
            "candidate_reject_reason": "official_auction_result_not_executable_quote",
        }
    ]


parse_cbb_treasury_bill_auction = parse_central_bank_of_bahrain_treasury_bill_auction


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
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "source_page_url": SOURCE_URL,
            "candidate_reject_reason": (
                "public_treasury_bill_parser_failure" if parser_error else "public_treasury_bill_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {
        **((root.get("adapters") or {}).get(adapter_id) or {}),
        **(root.get(adapter_id) or {}),
    }


class CentralBankOfBahrainTreasuryBillsAdapter:
    info = AdapterInfo(
        adapter_id="central_bank_of_bahrain_treasury_bills",
        venue=VENUE,
        market_type="treasury_bill_auction_reference",
        source="Central Bank of Bahrain Government Treasury Bill auction results",
        capabilities=(
            "public_market_data",
            "auction_results",
            "auction_price",
            "event_price_reference",
            "auction_yield",
            "lowest_accepted_price",
            "award_size",
            "issue_number",
            "source_health",
        ),
        aliases=(
            "central bank of bahrain",
            "cbb",
            "bahrain government treasury bills",
            "bahrain treasury bill auctions",
            "bahrain short end",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.central_bank_of_bahrain."
            "CentralBankOfBahrainTreasuryBillsAdapter"
        ),
        quote_assets=("BHD_PER_100_FACE",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        source_url = str(cfg.get("source_url") or RESULTS_URL)
        result = fetch_text(source_url, max(1, int(cfg.get("timeout_seconds", 15))))
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_central_bank_of_bahrain_treasury_bill_auction(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                    stale_after_hours=max(0.0, float(cfg.get("stale_after_hours", 168.0))),
                )
                source_status = "reachable"
            except (CentralBankOfBahrainTreasuryBillParseError, TypeError, ValueError) as exc:
                message = f"Central Bank of Bahrain Treasury-bill parser failed: {exc}"[:300]
                parser_failures.append({"source_url": source_url, "error": message})
                observations = [_failure_observation(result, source_url, message)]
                source_status = "degraded"

        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 679,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [
                    source_url,
                    SOURCE_URL,
                    TWELVE_MONTH_RESULTS_URL,
                    DEVELOPMENT_BOND_RESULTS_URL,
                ],
                "fetch_status": {"treasury_bill_results": _fetch_evidence(result, source_url)},
                "freshness_state": "fresh" if "fresh" in freshness_states else "stale" if "stale" in freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "entry_quality_secondary_market_bill_and_repo_quotes",
                "paper_only": True,
            },
        )


CentralBankOfBahrainAdapter = CentralBankOfBahrainTreasuryBillsAdapter


register_adapter(CentralBankOfBahrainTreasuryBillsAdapter())
