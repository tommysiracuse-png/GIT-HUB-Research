"""UAE Ministry of Finance Federal Debt Management Office issuance calendar.

The Ministry's public calendar identifies AED T-Bonds and T-Sukuk and their
planned tranches.  It is an official primary-market reference, rather than a
secondary-market quote or an execution venue, so every observation is kept
watch-only and paper-only.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, slug, utc_now
from scan_batch import ScanBatch


ISSUANCE_PROGRAMME_URL = "https://mof.gov.ae/en/public-finance/public-debt/issuance-programme/"
T_BONDS_URL = "https://mof.gov.ae/en/public-finance/public-debt/t-bonds/"
RETAIL_T_SUKUK_NEWS_URL = (
    "https://mof.gov.ae/en/news/ministry-of-finance-announces-opening-of-subscription-and-pricing-"
    "for-uaes-inaugural-sovereign-retail-t-sukuk-programme/"
)
SOURCE_URL = ISSUANCE_PROGRAMME_URL
VENUE = "UAE_MOF_FDMO"
MARKET_SURFACE = "uae_federal_t_bond_and_t_sukuk_issuance_programme"


class UaeFederalDebtIssuanceParseError(ValueError):
    """Raised if the official issuance-calendar table changes materially."""


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UaeFederalDebtIssuanceParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _calendar_year(document: str) -> int | None:
    found = re.search(r"(?:Issuance\s+Calendar\s*-?\s*Year|Year)\s+(20\d{2})", document, re.I)
    return int(found.group(1)) if found else None


def _date_iso(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y"):
        try:
            return dt.datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _aed_millions(value: Any) -> float | None:
    """Parse the calendar's AED-million figures, where commas are thousands separators."""

    text = str(value or "").strip().replace("\u00a0", " ")
    if not text or text in {"-", "â€“", "â€”", "N/A"}:
        return None
    normalized = re.sub(r"[^0-9.+-]", "", text.replace(",", ""))
    try:
        return float(normalized)
    except ValueError:
        return None


def _table_headers(table: list[list[str]]) -> list[str] | None:
    for row in table:
        joined = " ".join(row).lower()
        if "security type" in joined and "isin" in joined and "maturity date" in joined:
            return row
    return None


def _scheduled_tranches(row: list[str], headers: list[str]) -> list[dict[str, Any]]:
    # The final four columns are annual issuance, redemptions, current debt,
    # and year-end debt.  The intervening values are the scheduled auctions.
    tranches: list[dict[str, Any]] = []
    for index in range(4, max(4, len(row) - 4)):
        amount = _aed_millions(row[index])
        if amount is None or amount <= 0:
            continue
        tranches.append(
            {
                "auction_label": headers[index] if index < len(headers) else f"calendar_column_{index}",
                "amount_millions_aed": amount,
            }
        )
    return tranches


def parse_uae_federal_debt_issuance_programme(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the public UAE institutional and retail issuance calendars."""

    if not isinstance(document, str) or not document.strip():
        raise UaeFederalDebtIssuanceParseError("issuance programme response is empty")
    if not re.search(r"Issuance\s+Programme", document, re.I):
        raise UaeFederalDebtIssuanceParseError("Issuance Programme marker was not found")

    fetched_at = _received_time(received_at)
    calendar_year = _calendar_year(document)
    observations: list[dict[str, Any]] = []
    calendar_index = 0
    for table in html_tables(document):
        headers = _table_headers(table)
        if headers is None:
            continue
        normalized_headers = " ".join(headers).lower()
        if "total outstanding" not in normalized_headers:
            continue
        segment = "institutional" if calendar_index == 0 else "retail"
        calendar_index += 1
        for row in table:
            if len(row) < 7:
                continue
            security_type = re.sub(r"\s+", " ", row[0]).strip()
            if not re.fullmatch(r"T-(?:Bonds|Sukuk)", security_type, re.I):
                continue
            isin = re.sub(r"\s+", "", row[1]).upper()
            maturity_date = re.sub(r"\s+", " ", row[2]).strip()
            if not isin or not maturity_date:
                continue
            initial_outstanding = _aed_millions(row[3])
            annual_issuance = _aed_millions(row[-4])
            redemptions = _aed_millions(row[-3])
            current_outstanding = _aed_millions(row[-2])
            year_end_outstanding = _aed_millions(row[-1])
            instrument_key = isin if isin != "TBA" else f"{segment}:{maturity_date}"
            inst_id = f"{VENUE}:{slug(security_type)}:{slug(instrument_key)}"
            observations.append(
                {
                    "venue": VENUE,
                    "inst_id": inst_id,
                    "instrument_id": inst_id,
                    "symbol": isin,
                    "name": f"UAE {segment} {security_type} {maturity_date}",
                    "base": isin,
                    "quote": "AED_PER_100_FACE",
                    "market_type": "sovereign_debt_issuance_calendar_reference",
                    "market_surface": MARKET_SURFACE,
                    "asset_class": "sovereign_t_bond" if security_type.lower() == "t-bonds" else "sovereign_t_sukuk",
                    "trade_type": "official_primary_issuance_calendar",
                    "direction": "watch_only",
                    "last": 0.0,
                    "security_type": security_type,
                    "isin": isin,
                    "calendar_segment": segment,
                    "calendar_year": calendar_year,
                    "maturity_date": maturity_date,
                    "maturity_date_iso": _date_iso(maturity_date),
                    "initial_total_outstanding_domestic_debt_millions_aed": initial_outstanding,
                    "total_aed_issuances_for_year_millions": annual_issuance,
                    "redemptions_for_year_millions_aed": redemptions,
                    "total_outstanding_domestic_debt_millions_aed": current_outstanding,
                    "year_end_total_outstanding_domestic_debt_millions_aed": year_end_outstanding,
                    "scheduled_issuance_tranches": _scheduled_tranches(row, headers),
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_issuance_calendar_reference",
                    "freshness_state": "fresh",
                    "freshness_basis": "official_issuance_programme_page_fetch_timestamp",
                    "freshness_age_seconds": 0.0,
                    "session_status": f"{segment}_issuance_calendar",
                    "observed_at": fetched_at.isoformat(),
                    "fetched_at": fetched_at.isoformat(),
                    "price_source": "UAE Ministry of Finance issuance programme",
                    "source_url": source_url,
                    "candidate_reject_reason": "official_issuance_calendar_has_no_secondary_market_quote",
                }
            )
    if not observations:
        raise UaeFederalDebtIssuanceParseError("no T-Bonds or T-Sukuk issuance rows were found")
    return observations


parse_uae_issuance_programme = parse_uae_federal_debt_issuance_programme


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
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "market_type": "sovereign_debt_issuance_calendar_reference",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_issuance_calendar_parser_failure"
                if parser_error
                else "public_issuance_calendar_source_unavailable"
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


class MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter:
    info = AdapterInfo(
        adapter_id="ministry_of_finance_uae_federal_debt_management_office",
        venue=VENUE,
        market_type="sovereign_debt_issuance_calendar_reference",
        source="UAE Ministry of Finance Federal Debt Management Office issuance programme",
        capabilities=(
            "public_market_data",
            "sovereign_debt_issuance_calendar",
            "t_bond_issuance",
            "t_sukuk_issuance",
            "isin_catalog",
            "maturity_schedule",
            "outstanding_debt_reference",
            "source_health",
        ),
        aliases=(
            "ministry of finance uae",
            "uae federal debt management office",
            "federal debt management office",
            "uae t-bonds",
            "uae t-sukuk",
            "uae issuance programme",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint=(
            "adapters.venues.ministry_of_finance_uae_federal_debt_management_office."
            "MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter"
        ),
        quote_assets=("AED_PER_100_FACE",),
        default_cache_minutes=180,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        source_url = str(cfg.get("source_url") or SOURCE_URL)
        result = fetch_text(source_url, max(1, int(cfg.get("timeout_seconds", 15))))
        parser_failures: list[dict[str, str]] = []
        if not result.get("ok"):
            observations = [_failure_observation(result, source_url)]
            source_status = str(result.get("status") or "unavailable")
        else:
            try:
                observations = parse_uae_federal_debt_issuance_programme(
                    str(result.get("text") or ""),
                    source_url=source_url,
                    received_at=result.get("received_at"),
                )
                source_status = "reachable"
            except (UaeFederalDebtIssuanceParseError, TypeError, ValueError) as exc:
                message = f"UAE issuance programme parser failed: {exc}"[:300]
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
                "adapter_spec_id": 674,
                "source_status": source_status,
                "source_url": source_url,
                "source_urls": [source_url, SOURCE_URL, T_BONDS_URL, RETAIL_T_SUKUK_NEWS_URL],
                "fetch_status": {"issuance_programme": _fetch_evidence(result, source_url)},
                "freshness_state": "fresh" if "fresh" in freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "capability_gap": "public_secondary_market_indications_and_benchmark_curve_quotes",
                "paper_only": True,
            },
        )


UaeFederalDebtManagementOfficeAdapter = MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter


register_adapter(MinistryOfFinanceUaeFederalDebtManagementOfficeAdapter())
