"""Bank of Botswana monetary-policy implementation reference adapter.

The Bank's framework page publishes the operational terms for 7-day and
one-month BoBCs and the SDF/SCF corridor.  Those are official policy and
auction references, not executable market quotes, so this plugin is strictly
watch-only and paper-only.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from html.parser import HTMLParser
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


FRAMEWORK_URL = "https://www.bankofbotswana.bw/content/monetary-policy-implementation-framework"
MPC_DECISION_URL = (
    "https://www.bankofbotswana.bw/sites/default/files/news-files/"
    "Monetary%20Policy%20Committee%20Decision%20-%20June%202025.pdf"
)
MARKET_SURFACE = "botswana_short_end_policy_and_bobc_references"
VENUE = "BANK_OF_BOTSWANA"


class BankOfBotswanaParseError(ValueError):
    """Raised when an official Bank of Botswana response loses required facts."""


class _VisibleTextParser(HTMLParser):
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


def _visible_text(document: str) -> str:
    if not isinstance(document, str) or not document.strip():
        raise BankOfBotswanaParseError("official response is empty")
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - upstream drift must remain health evidence.
        raise BankOfBotswanaParseError(f"invalid HTML response: {exc}") from exc
    text = " ".join(html.unescape(" ".join(parser.parts)).split())
    if not text:
        raise BankOfBotswanaParseError("official response has no visible text")
    return text


def extract_pdf_text(body: bytes) -> str:
    """Extract visible text from the public MPC decision PDF."""

    if not isinstance(body, bytes) or not body:
        raise BankOfBotswanaParseError("MPC decision PDF response is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared autonomous dependency.
        raise BankOfBotswanaParseError("pypdf is required to read the MPC decision PDF") from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - upstream PDF changes are source evidence.
        raise BankOfBotswanaParseError(f"MPC decision PDF could not be read: {exc}") from exc
    if not text.strip():
        raise BankOfBotswanaParseError("MPC decision PDF contains no extractable text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BankOfBotswanaParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _rate(text: str, label: str, patterns: tuple[str, ...]) -> float:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if 0.0 <= value <= 100.0:
                return value
    raise BankOfBotswanaParseError(f"{label} rate was not found")


def _policy_rates(text: str) -> tuple[float, float, float]:
    mop = _rate(
        text,
        "MoPR",
        (
            r"(?:Monetary\s+Policy\s+Rate\s*\(\s*MoPR\s*\)|MoPR)[\s\S]{0,180}?\b(?:at|to|of)\s+(\d+(?:\.\d+)?)\s*(?:per\s*cent|percent|%)",
            r"\bMoPR\s+(\d+(?:\.\d+)?)\s*(?:per\s*cent|percent|%)",
        ),
    )
    sdf = _rate(
        text,
        "SDF",
        (
            r"(?:Standing\s+Deposit\s+Facility\s*\(\s*SDF\s*\)|SDF)[\s\S]{0,180}?\b(?:at|to|of)\s+(\d+(?:\.\d+)?)\s*(?:per\s*cent|percent|%)",
        ),
    )
    scf = _rate(
        text,
        "SCF",
        (
            r"(?:Standing\s+Credit\s+Facility\s*\(\s*SCF\s*\)|SCF)[\s\S]{0,180}?\b(?:at|to|of)\s+(\d+(?:\.\d+)?)\s*(?:per\s*cent|percent|%)",
        ),
    )
    return mop, sdf, scf


def _meeting_date(text: str) -> dt.date | None:
    match = re.search(r"(?:meeting(?:\s+of|\s+held\s+on)?|Meets)\s+(?:on\s+)?(\d{1,2}\s+[A-Za-z]+\s+20\d{2})", text, re.I)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%d %B %Y").date()
    except ValueError:
        return None


def _framework_freshness(
    fetched_at: dt.datetime, effective_date: dt.date | None, stale_after_days: float
) -> tuple[str, float]:
    if not effective_date:
        return "unknown", 0.0
    effective_at = dt.datetime.combine(effective_date, dt.time.min, tzinfo=dt.timezone.utc)
    age_seconds = max(0.0, (fetched_at - effective_at).total_seconds())
    return (
        "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400.0 else "stale",
        round(age_seconds, 3),
    )


def _reference_observation(
    *,
    symbol: str,
    name: str,
    rate_pct: float | None,
    source_url: str,
    fetched_at: dt.datetime,
    freshness_state: str,
    freshness_age_seconds: float | None,
    session_status: str,
    **details: Any,
) -> dict[str, Any]:
    inst_id = f"{VENUE}:{symbol}"
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": symbol,
        "name": name,
        "base": symbol,
        "quote": "BWP_RATE_PCT",
        "market_type": "central_bank_money_market_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": "money_market_policy_reference",
        "trade_type": "official_monetary_policy_reference",
        "direction": "watch_only",
        "last": rate_pct,
        "rate_pct": rate_pct,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_policy_reference",
        "freshness_state": freshness_state,
        "freshness_basis": "official_policy_effective_date",
        "freshness_age_seconds": freshness_age_seconds,
        "session_status": session_status,
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Bank of Botswana official monetary policy publication",
        "source_url": source_url,
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": rate_pct is not None,
        **details,
    }


def parse_bank_of_botswana_monetary_policy_framework(
    document: str,
    *,
    source_url: str = FRAMEWORK_URL,
    received_at: str | None = None,
    stale_after_days: float = 180.0,
) -> list[dict[str, Any]]:
    """Normalize BoBC terms and the SDF/SCF corridor from the framework page."""

    text = _visible_text(document)
    required_markers = (
        "Bank of Botswana Certificates",
        "7-day",
        "1-month",
        "Standing Deposit Facility",
        "Standing Credit Facility",
    )
    missing = [marker for marker in required_markers if marker.lower() not in text.lower()]
    if missing:
        raise BankOfBotswanaParseError("framework missing markers: " + ", ".join(missing))
    mop, sdf, scf = _policy_rates(text)
    if round(mop - sdf, 6) != 1.0 or round(scf - mop, 6) != 1.0:
        raise BankOfBotswanaParseError("SDF/SCF rates do not form the documented 100-basis-point corridor")
    if not re.search(r"7-day\s+BoBCs?.{0,180}?(?:weekly|Tuesdays?).{0,180}?MoPR", text, re.I):
        raise BankOfBotswanaParseError("7-day BoBC weekly MoPR auction terms were not found")
    if not re.search(r"1-month\s+BoBCs?.{0,180}?third\s+Tuesday", text, re.I):
        raise BankOfBotswanaParseError("1-month BoBC third-Tuesday auction terms were not found")
    fetched_at = _received_time(received_at)
    effective_date = _meeting_date(text)
    freshness_state, age_seconds = _framework_freshness(fetched_at, effective_date, stale_after_days)
    common = {
        "mopr_pct": mop,
        "sdf_pct": sdf,
        "scf_pct": scf,
        "corridor_width_bps": 200,
        "corridor_half_width_bps": 100,
        "policy_effective_date": effective_date.isoformat() if effective_date else None,
    }
    return [
        _reference_observation(
            symbol="BOBC_7D",
            name="Bank of Botswana Certificate - 7-day",
            rate_pct=mop,
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="weekly_auction_tuesday_t_plus_1",
            instrument_term_days=7,
            auction_frequency="weekly_tuesday",
            auction_method="fixed_rate_full_allotment",
            settlement_cycle="T+1",
            **common,
        ),
        _reference_observation(
            symbol="BOBC_1M",
            name="Bank of Botswana Certificate - 1-month",
            rate_pct=None,
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="monthly_auction_third_tuesday_t_plus_1",
            instrument_term_days=30,
            auction_frequency="third_tuesday_monthly",
            auction_method="multiple_price_predetermined_volume",
            settlement_cycle="T+1",
            reference_policy_rate_pct=mop,
            **common,
        ),
        _reference_observation(
            symbol="SDF_OVERNIGHT",
            name="Standing Deposit Facility - overnight",
            rate_pct=sdf,
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="available_until_1815_biss",
            facility_type="standing_deposit",
            maturity="overnight",
            settlement_cycle="T+0",
            rate_spread_to_mopr_bps=-100,
            **common,
        ),
        _reference_observation(
            symbol="SCF_OVERNIGHT",
            name="Standing Credit Facility - overnight",
            rate_pct=scf,
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="available_until_1730_biss",
            facility_type="standing_credit",
            maturity="overnight",
            settlement_cycle="T+0",
            transaction_basis="repo",
            rate_spread_to_mopr_bps=100,
            **common,
        ),
    ]


def parse_bank_of_botswana_mpc_decision(
    document: str,
    *,
    source_url: str = MPC_DECISION_URL,
    received_at: str | None = None,
    stale_after_days: float = 180.0,
) -> list[dict[str, Any]]:
    """Normalize the supplied MPC decision as a dated policy snapshot."""

    if not isinstance(document, str) or not document.strip():
        raise BankOfBotswanaParseError("MPC decision response is empty")
    text = " ".join(document.split())
    if "Monetary Policy Committee" not in text or "DECISION" not in text.upper():
        raise BankOfBotswanaParseError("MPC decision markers were not found")
    meeting_date = _meeting_date(text)
    if not meeting_date:
        raise BankOfBotswanaParseError("MPC decision date was not found")
    mop, sdf, scf = _policy_rates(text)
    if round(mop - sdf, 6) != 1.0 or round(scf - mop, 6) != 1.0:
        raise BankOfBotswanaParseError("MPC decision rates do not form a 100-basis-point corridor")
    fetched_at = _received_time(received_at)
    freshness_state, age_seconds = _framework_freshness(fetched_at, meeting_date, stale_after_days)
    return [
        _reference_observation(
            symbol=f"MPC_DECISION_{meeting_date.isoformat()}",
            name=f"Bank of Botswana MPC decision - {meeting_date.isoformat()}",
            rate_pct=mop,
            source_url=source_url,
            fetched_at=fetched_at,
            freshness_state=freshness_state,
            freshness_age_seconds=age_seconds,
            session_status="mpc_decision_published",
            decision_date=meeting_date.isoformat(),
            mopr_pct=mop,
            sdf_pct=sdf,
            scf_pct=scf,
            corridor_width_bps=200,
            corridor_half_width_bps=100,
            seven_day_bobc_rate_pct=mop,
            repo_rate_pct=mop,
            repo_tenor="up_to_one_month",
        )
    ]


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
    source_key: str, source_url: str, result: dict[str, Any], parser_error: str | None = None
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "instrument_id": f"{VENUE}:{source_key.upper()}:HEALTH",
            "symbol": f"{source_key.upper()}_HEALTH",
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "candidate_reject_reason": (
                "public_botswana_policy_parser_failure"
                if parser_error
                else "public_botswana_policy_source_unavailable"
            ),
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class BankOfBotswanaAdapter:
    info = AdapterInfo(
        adapter_id="bank_of_botswana_monetary_policy",
        venue=VENUE,
        market_type="central_bank_money_market_reference",
        source="Bank of Botswana BoBC and standing-facility policy references",
        capabilities=(
            "public_market_data",
            "event_price_reference",
            "policy_rate",
            "money_market_reference",
            "auction_schedule",
            "standing_facility_corridor",
            "source_health",
        ),
        aliases=(
            "bank of botswana",
            "bob",
            "bobc",
            "bank of botswana certificates",
            "botswana short end",
            "standing deposit facility",
            "standing credit facility",
        ),
        docs_url=FRAMEWORK_URL,
        runtime_entrypoint="adapters.venues.bank_of_botswana.BankOfBotswanaAdapter",
        quote_assets=("BWP_RATE_PCT",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 180.0)))
        framework_url = str(cfg.get("framework_source_url") or FRAMEWORK_URL)
        mpc_url = str(cfg.get("mpc_decision_source_url") or MPC_DECISION_URL)
        sources = (
            ("framework", framework_url, fetch_text, parse_bank_of_botswana_monetary_policy_framework),
            ("mpc_decision", mpc_url, fetch_bytes, parse_bank_of_botswana_mpc_decision),
        )
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        for source_key, source_url, fetcher, parser in sources:
            result = fetcher(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                if source_key == "framework":
                    document = str(result.get("text") or "")
                else:
                    document = str(result["text"]) if result.get("text") else extract_pdf_text(result.get("content") or b"")
                observations.extend(
                    parser(
                        document,
                        source_url=source_url,
                        received_at=result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
                usable_sources += 1
            except (BankOfBotswanaParseError, TypeError, ValueError) as exc:
                message = f"Bank of Botswana {source_key} parser failed: {exc}"[:300]
                parser_failures.append({"source_key": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        if usable_sources == len(sources) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        else:
            statuses = {item["fetch_status"] for item in fetch_status.values()}
            source_status = statuses.pop() if len(statuses) == 1 else "unavailable"
        real_rows = [row for row in observations if row.get("quality_status") == "official_policy_reference"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 851,
                "source_status": source_status,
                "source_urls": [framework_url, mpc_url],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "capability_gap": "public_current_bobc_auction_results_and_executable_bwp_money_market_quotes",
                "paper_only": True,
            },
        )


register_adapter(BankOfBotswanaAdapter())
