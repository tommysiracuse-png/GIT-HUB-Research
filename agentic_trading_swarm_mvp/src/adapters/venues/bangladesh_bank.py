"""Bangladesh Bank Treasury-bill and Treasury-bond auction references.

Bangladesh Bank publishes a public auction calendar and links result PDFs from
its press-release index.  Those records are useful primary-market curve
evidence, but they are not executable secondary-market quotes.  Accordingly
this adapter emits watch-only, paper-only observations and never candidates or
order instructions.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_bytes, fetch_text, health_observation, html_tables, number, utc_now
from scan_batch import ScanBatch


AUCTION_CALENDAR_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/auc_calendar"
AUCTION_NOTICE_URL = "https://www.bb.org.bd/en/index.php/monetaryactivity/auc_notice"
PRESS_RELEASE_URL = "https://www.bb.org.bd/en/index.php/mediaroom/press_release"
SECONDARY_MARKET_URL = "https://www.bb.org.bd/fnansys/govsecmrkt/secondarymrkt.php"
MARKET_SURFACE = "bangladesh_government_treasury_bill_and_bond_auctions"
VENUE = "BANGLADESH_BANK"
_DHAKA_TIME = dt.timezone(dt.timedelta(hours=6), name="Asia/Dhaka")


class BangladeshBankParseError(ValueError):
    """Raised when a reachable official page loses required auction fields."""


def extract_pdf_text(body: bytes) -> str:
    """Extract text from a public Bangladesh Bank result PDF."""

    if not isinstance(body, bytes) or not body:
        raise BangladeshBankParseError("auction result PDF is empty")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - declared dependency.
        raise BangladeshBankParseError("pypdf is required to read auction result PDFs") from exc
    try:
        text = "\n".join(str(page.extract_text() or "") for page in PdfReader(io.BytesIO(body)).pages)
    except Exception as exc:  # noqa: BLE001 - source drift is retained as health evidence.
        raise BangladeshBankParseError(f"auction result PDF could not be read: {exc}") from exc
    if not text.strip():
        raise BangladeshBankParseError("auction result PDF contains no extractable text")
    return text


def _received_time(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BangladeshBankParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _date(value: str, field: str) -> dt.date:
    cleaned = " ".join(str(value or "").replace(".", " ").split())
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    raise BangladeshBankParseError(f"invalid {field}: {value!r}")


def _title_tenor(value: str) -> tuple[str, int, str] | None:
    text = " ".join(str(value or "").upper().split())
    if "FRTB" in text or "FLOATING RATE TREASURY BOND" in text:
        return "3Y_FRTB", 3 * 365, "floating_rate_treasury_bond"
    bill = re.search(r"\b(91|182|364)\s*(?:-| )?DAYS?\b", text)
    if bill:
        days = int(bill.group(1))
        return f"TBILL_{days}D", days, "treasury_bill"
    bond = re.search(r"\b(2|5|10|15|20)\s*(?:-| )?(?:YR|Y(?:EAR)?S?)\b", text)
    if bond:
        years = int(bond.group(1))
        return f"BGTB_{years}Y", years * 365, "treasury_bond"
    return None


def _calendar_session(auction_date: dt.date, fetched_at: dt.datetime) -> str:
    local_day = fetched_at.astimezone(_DHAKA_TIME).date()
    if auction_date > local_day:
        return "auction_scheduled"
    if auction_date == local_day:
        return "auction_day"
    return "calendar_historical"


def _calendar_tables(document: str) -> list[list[list[str]]]:
    """Read both conventional tables and Bangladesh Bank's div-table markup."""

    tables = html_tables(document)
    column_pattern = re.compile(
        r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\bcolumn\b[^"\']*["\'][^>]*>(.*?)</div\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    captions = list(re.finditer(r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\btable_caption\b[^"\']*["\'][^>]*>', document, re.I))
    for index, caption in enumerate(captions):
        section_end = captions[index + 1].start() if index + 1 < len(captions) else len(document)
        section = document[caption.start() : section_end]
        header = re.search(r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\brow-header\b[^"\']*["\'][^>]*>(.*?)(?=<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\brow-data\b)', section, re.I | re.S)
        if not header:
            continue
        headings = [" ".join(re.sub(r"<[^>]+>", " ", html.unescape(item)).split()) for item in column_pattern.findall(header.group(1))]
        if not headings:
            continue
        lines = [headings]
        row_pattern = re.compile(
            r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\brow-data\b[^"\']*["\'][^>]*>(.*?)(?=<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\brow-data\b|$)',
            re.IGNORECASE | re.DOTALL,
        )
        for row in row_pattern.finditer(section):
            cells = [" ".join(re.sub(r"<[^>]+>", " ", html.unescape(item)).split()) for item in column_pattern.findall(row.group(1))]
            if cells:
                lines.append(cells)
        if len(lines) > 1:
            tables.append(lines)
    return tables


def _calendar_observation(
    *,
    auction_number: str,
    auction_date: dt.date,
    tenor: str,
    term_days: int,
    asset_class: str,
    announced_amount: float,
    total_amount: float | None,
    source_url: str,
    fetched_at: dt.datetime,
) -> dict[str, Any]:
    inst_id = f"{VENUE}:{tenor}:CALENDAR:{auction_date.isoformat()}"
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": tenor,
        "name": f"Bangladesh Government {tenor.replace('_', ' ')} auction calendar",
        "base": tenor,
        "quote": "BDT_CRORE",
        "market_type": "treasury_auction_calendar_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": asset_class,
        "trade_type": "official_primary_auction_calendar",
        "direction": "watch_only",
        "last": announced_amount,
        "auction_key": f"{auction_date.isoformat()}:{tenor}",
        "auction_number": auction_number or None,
        "auction_date": auction_date.isoformat(),
        "auction_at": dt.datetime.combine(auction_date, dt.time.min, tzinfo=_DHAKA_TIME).isoformat(),
        "auction_term_days": term_days,
        "announced_supply_crore_bdt": announced_amount,
        "calendar_total_supply_crore_bdt": total_amount,
        "price_available": False,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_auction_calendar",
        "freshness_state": "fresh",
        "freshness_basis": "official_auction_calendar_fetch",
        "freshness_age_seconds": 0.0,
        "session_status": _calendar_session(auction_date, fetched_at),
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Bangladesh Bank official auction calendar",
        "source_url": source_url,
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": False,
        "candidate_reject_reason": "official_auction_calendar_not_executable_quote",
    }


def parse_bangladesh_bank_auction_calendar(
    document: str,
    *,
    source_url: str = AUCTION_CALENDAR_URL,
    received_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize published 91d/182d/364d bill and BGTB auction supply."""

    if not isinstance(document, str) or not document.strip():
        raise BangladeshBankParseError("auction calendar response is empty")
    fetched_at = _received_time(received_at)
    rows: list[dict[str, Any]] = []
    for table in _calendar_tables(document):
        if not table:
            continue
        header_index = next(
            (
                index
                for index, line in enumerate(table)
                if any("auction date" in str(cell).lower() for cell in line)
                and any("total amount" in str(cell).lower() for cell in line)
            ),
            None,
        )
        if header_index is None:
            continue
        headers = [" ".join(cell.lower().split()) for cell in table[header_index]]
        term_columns: list[tuple[int, str, int, str]] = []
        for index, header in enumerate(headers):
            identity = _title_tenor(header)
            if identity:
                tenor, days, asset_class = identity
                term_columns.append((index, tenor, days, asset_class))
        date_index = next((index for index, header in enumerate(headers) if "auction date" in header), -1)
        number_index = next((index for index, header in enumerate(headers) if "auction no" in header), -1)
        total_index = next((index for index, header in enumerate(headers) if "total amount" in header), -1)
        if date_index < 0 or not term_columns:
            continue
        for line in table[header_index + 1 :]:
            if len(line) <= date_index or not str(line[date_index]).strip():
                continue
            try:
                auction_date = _date(str(line[date_index]), "auction date")
            except BangladeshBankParseError:
                continue
            total_amount = number(line[total_index]) if total_index >= 0 and len(line) > total_index else None
            auction_number = str(line[number_index]).strip() if number_index >= 0 and len(line) > number_index else ""
            for index, tenor, days, asset_class in term_columns:
                amount = number(line[index]) if len(line) > index else None
                if amount is None or amount <= 0:
                    continue
                rows.append(
                    _calendar_observation(
                        auction_number=auction_number,
                        auction_date=auction_date,
                        tenor=tenor,
                        term_days=days,
                        asset_class=asset_class,
                        announced_amount=amount,
                        total_amount=total_amount,
                        source_url=source_url,
                        fetched_at=fetched_at,
                    )
                )
    if not rows:
        raise BangladeshBankParseError("calendar did not contain supported bill or bond supply rows")
    return sorted(rows, key=lambda row: (str(row["auction_date"]), str(row["symbol"])))


def parse_bangladesh_bank_auction_result_links(
    document: str,
    *,
    limit: int = 6,
) -> list[dict[str, str]]:
    """Return recent official G-Sec result PDF links from the release index."""

    if not isinstance(document, str) or not document.strip():
        raise BangladeshBankParseError("press-release index response is empty")
    links: list[dict[str, str]] = []
    for match in re.finditer(r"<tr\b[^>]*>(.*?)</tr\s*>", document, re.IGNORECASE | re.DOTALL):
        row = match.group(1)
        url = re.search(r"\bpdf-link\s*=\s*[\"']\s*([^\"']+?\.pdf)\s*[\"']", row, re.I)
        if not url:
            continue
        cells = re.findall(r"<td\b[^>]*>(.*?)</td\s*>", row, re.IGNORECASE | re.DOTALL)
        if len(cells) < 3:
            continue
        title = re.sub(r"<[^>]+>", " ", html.unescape(cells[2])).replace("\xa0", " ")
        title = " ".join(title.split())
        lower = title.casefold()
        relevant = (
            ("treasury bills auction" in lower and "held on" in lower)
            or ("treasury bond auction" in lower and "result" in lower)
            or ("floating rate treasury bond auction result" in lower)
        )
        if not relevant:
            continue
        published = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(cells[1])).split())
        links.append({"source_url": html.unescape(url.group(1)).strip(), "title": title, "published_date": published})
    if not links:
        raise BangladeshBankParseError("press-release index did not contain Treasury Bill or Bond result PDFs")
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        if link["source_url"] not in seen:
            unique.append(link)
            seen.add(link["source_url"])
    return unique[: max(1, int(limit))]


def _result_date(text: str) -> dt.date:
    found = re.search(r"\bheld\s+on\s+(\d{1,2}\s+[A-Za-z]+\s+20\d{2})\b", text, re.I)
    if not found:
        raise BangladeshBankParseError("auction result date was not found")
    return _date(found.group(1), "auction result date")


def _numeric_values(value: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"\d[\d,]*(?:\.\d+)?", value)]


def _result_observation(
    *,
    tenor: str,
    term_days: int,
    asset_class: str,
    auction_date: dt.date,
    announced: float,
    offered_count: float,
    offered_amount: float,
    accepted_count: float,
    accepted_amount: float,
    settlement_amount: float,
    weighted_yield: float,
    cutoff_yield: float,
    source_url: str,
    fetched_at: dt.datetime,
    extra: dict[str, Any],
    stale_after_days: float,
) -> dict[str, Any]:
    auction_at = dt.datetime.combine(auction_date, dt.time.min, tzinfo=_DHAKA_TIME)
    age = max(0.0, (fetched_at - auction_at.astimezone(dt.timezone.utc)).total_seconds())
    inst_id = f"{VENUE}:{tenor}:AUCTION:{auction_date.isoformat()}"
    return {
        "venue": VENUE,
        "inst_id": inst_id,
        "instrument_id": inst_id,
        "symbol": tenor,
        "name": f"Bangladesh Government {tenor.replace('_', ' ')} auction result",
        "base": tenor,
        "quote": "BDT_YIELD_PCT",
        "market_type": "treasury_auction_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": asset_class,
        "trade_type": "official_primary_auction_result",
        "direction": "watch_only",
        "last": weighted_yield,
        "auction_key": f"{auction_date.isoformat()}:{tenor}",
        "auction_date": auction_date.isoformat(),
        "auction_at": auction_at.isoformat(),
        "auction_term_days": term_days,
        "announced_supply_crore_bdt": announced,
        "auction_offered_bid_count": int(offered_count),
        "auction_offered_amount_crore_bdt": offered_amount,
        "auction_accepted_bid_count": int(accepted_count),
        "awarded_amount_crore_bdt": accepted_amount,
        "settlement_amount_bdt": settlement_amount,
        "auction_coverage_ratio": round(offered_amount / announced, 6) if announced > 0 else None,
        "auction_weighted_average_yield_pct": weighted_yield,
        "auction_average_yield_pct": weighted_yield,
        "auction_stop_out_yield_pct": cutoff_yield,
        "auction_result_published": 1.0,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_auction_result",
        "freshness_state": "fresh" if age <= max(0.0, stale_after_days) * 86400 else "stale",
        "freshness_basis": "official_auction_result_date",
        "freshness_age_seconds": round(age, 3),
        "session_status": "results_published",
        "observed_at": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Bangladesh Bank official Treasury auction result",
        "source_url": source_url,
        "paper_route": "synthetic_reference",
        "execution_mode": "paper_only",
        "paper_experiment_eligible": True,
        "candidate_reject_reason": "official_auction_result_not_executable_quote",
        **extra,
    }


def parse_bangladesh_bank_auction_results(
    document: str,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 180.0,
) -> list[dict[str, Any]]:
    """Normalize a public Treasury Bill or BGTB result PDF's published fields."""

    if not isinstance(document, str) or not document.strip():
        raise BangladeshBankParseError("auction result document is empty")
    normalized = "\n".join(line.strip() for line in document.replace("\r", "\n").splitlines() if line.strip())
    lower = normalized.casefold()
    if "bangladesh bank" not in lower or "auction" not in lower or "held on" not in lower:
        raise BangladeshBankParseError("auction result document markers were not found")
    auction_date = _result_date(normalized)
    fetched_at = _received_time(received_at)
    results: list[dict[str, Any]] = []
    is_bill = "treasury bills auctions" in lower or bool(re.search(r"\b91\s*-\s*days\b", normalized, re.I))
    if is_bill:
        for match in re.finditer(r"(?im)^\s*(91|182|364)\s*-\s*days\s+(.+)$", normalized):
            days = int(match.group(1))
            values = _numeric_values(match.group(2))
            if len(values) < 12:
                continue
            announced, offered_count, offered_amount, max_price, min_price, accepted_count, accepted_amount, settlement, weighted_price, weighted_yield, cutoff_price, cutoff_yield = values[:12]
            results.append(
                _result_observation(
                    tenor=f"TBILL_{days}D",
                    term_days=days,
                    asset_class="treasury_bill",
                    auction_date=auction_date,
                    announced=announced,
                    offered_count=offered_count,
                    offered_amount=offered_amount,
                    accepted_count=accepted_count,
                    accepted_amount=accepted_amount,
                    settlement_amount=settlement,
                    weighted_yield=weighted_yield,
                    cutoff_yield=cutoff_yield,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    stale_after_days=stale_after_days,
                    extra={
                        "auction_max_offer_price_per_100": max_price,
                        "auction_min_offer_price_per_100": min_price,
                        "auction_weighted_average_price_per_100": weighted_price,
                        "auction_cutoff_price_per_100": cutoff_price,
                    },
                )
            )
    else:
        pattern = r"(?im)^\s*((?:2|5|10|15|20)\s*-\s*year\s+BGTB|3\s*-\s*year.*?(?:FRTB|FLOATING\s+RATE\s+TREASURY\s+BOND))\s+(.+)$"
        for match in re.finditer(pattern, normalized):
            identity = _title_tenor(match.group(1))
            values = _numeric_values(match.group(2))
            if not identity or len(values) < 11:
                continue
            tenor, days, asset_class = identity
            announced, offered_count, offered_amount, minimum_yield, maximum_yield, accepted_count, accepted_amount, settlement, coupon, cutoff_yield, weighted_yield = values[:11]
            results.append(
                _result_observation(
                    tenor=tenor,
                    term_days=days,
                    asset_class=asset_class,
                    auction_date=auction_date,
                    announced=announced,
                    offered_count=offered_count,
                    offered_amount=offered_amount,
                    accepted_count=accepted_count,
                    accepted_amount=accepted_amount,
                    settlement_amount=settlement,
                    weighted_yield=weighted_yield,
                    cutoff_yield=cutoff_yield,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    stale_after_days=stale_after_days,
                    extra={
                        "coupon_rate_pct": coupon,
                        "auction_minimum_offered_yield_pct": minimum_yield,
                        "auction_maximum_offered_yield_pct": maximum_yield,
                    },
                )
            )
    if not results:
        raise BangladeshBankParseError("auction result contained no supported bill or bond rows")
    return results


def _fetch_evidence(result: dict[str, Any], source_url: str) -> dict[str, Any]:
    return {
        "source_url": source_url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
        "error": str(result.get("error") or "")[:300] or None,
    }


def _failure_observation(source_key: str, source_url: str, result: dict[str, Any], parser_error: str | None = None) -> dict[str, Any]:
    evidence = {**result, **({"status": "degraded", "error": parser_error} if parser_error else {})}
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
            "candidate_reject_reason": "public_bangladesh_bank_parser_failure" if parser_error else "public_bangladesh_bank_source_unavailable",
        }
    )
    return row


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    return {**((root.get("adapters") or {}).get(adapter_id) or {}), **(root.get(adapter_id) or {})}


class BangladeshBankTreasuryAuctionsAdapter:
    info = AdapterInfo(
        adapter_id="bangladesh_bank_treasury_auctions",
        venue=VENUE,
        market_type="treasury_auction_reference",
        source="Bangladesh Bank Treasury Bill and Bangladesh Government Treasury Bond auctions",
        capabilities=(
            "public_market_data",
            "auction_schedule",
            "auction_notice",
            "auction_results",
            "auction_yield",
            "auction_price",
            "award_size",
            "event_price_reference",
            "source_health",
        ),
        aliases=(
            "bangladesh bank",
            "bangladesh treasury bills",
            "bangladesh government treasury bonds",
            "bgtb",
            "frtb",
            "bangladesh treasury auctions",
        ),
        docs_url=AUCTION_CALENDAR_URL,
        runtime_entrypoint="adapters.venues.bangladesh_bank.BangladeshBankTreasuryAuctionsAdapter",
        quote_assets=("BDT_YIELD_PCT", "BDT_CRORE"),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_days = max(0.0, float(cfg.get("stale_after_days", 180.0)))
        result_limit = max(1, min(int(cfg.get("max_result_documents", 6)), 12))
        calendar_url = str(cfg.get("auction_calendar_url") or AUCTION_CALENDAR_URL)
        press_release_url = str(cfg.get("press_release_url") or PRESS_RELEASE_URL)
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0

        calendar = fetch_text(calendar_url, timeout)
        fetch_status["auction_calendar"] = _fetch_evidence(calendar, calendar_url)
        if not calendar.get("ok"):
            observations.append(_failure_observation("auction_calendar", calendar_url, calendar))
        else:
            try:
                observations.extend(parse_bangladesh_bank_auction_calendar(str(calendar.get("text") or ""), source_url=calendar_url, received_at=calendar.get("received_at")))
                usable_sources += 1
            except (BangladeshBankParseError, TypeError, ValueError) as exc:
                message = f"Bangladesh Bank auction-calendar parser failed: {exc}"[:300]
                parser_failures.append({"source_key": "auction_calendar", "source_url": calendar_url, "error": message})
                observations.append(_failure_observation("auction_calendar", calendar_url, calendar, message))

        index = fetch_text(press_release_url, timeout)
        fetch_status["press_release_index"] = _fetch_evidence(index, press_release_url)
        result_links: list[dict[str, str]] = []
        if not index.get("ok"):
            observations.append(_failure_observation("press_release_index", press_release_url, index))
        else:
            try:
                result_links = parse_bangladesh_bank_auction_result_links(str(index.get("text") or ""), limit=result_limit)
                usable_sources += 1
            except (BangladeshBankParseError, TypeError, ValueError) as exc:
                message = f"Bangladesh Bank press-release index parser failed: {exc}"[:300]
                parser_failures.append({"source_key": "press_release_index", "source_url": press_release_url, "error": message})
                observations.append(_failure_observation("press_release_index", press_release_url, index, message))

        for sequence, link in enumerate(result_links, start=1):
            source_key = f"auction_result_{sequence}"
            source_url = link["source_url"]
            result = fetch_bytes(source_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, source_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, source_url, result))
                continue
            try:
                document = str(result["text"]) if result.get("text") else extract_pdf_text(result.get("content") or b"")
                observations.extend(parse_bangladesh_bank_auction_results(document, source_url=source_url, received_at=result.get("received_at"), stale_after_days=stale_after_days))
                usable_sources += 1
            except (BangladeshBankParseError, TypeError, ValueError) as exc:
                message = f"Bangladesh Bank auction-result parser failed: {exc}"[:300]
                parser_failures.append({"source_key": source_key, "source_url": source_url, "error": message})
                observations.append(_failure_observation(source_key, source_url, result, message))

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(fetch_status) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"
        real_rows = [row for row in observations if str(row.get("quality_status", "")).startswith("official_")]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 293,
                "source_status": source_status,
                "source_url": calendar_url,
                "source_urls": [calendar_url, AUCTION_NOTICE_URL, press_release_url, SECONDARY_MARKET_URL],
                "fetch_status": fetch_status,
                "freshness_state": freshness_states[0] if len(freshness_states) == 1 else "mixed" if freshness_states else "unknown",
                "freshness_states": freshness_states,
                "session_state": session_states[0] if len(session_states) == 1 else "mixed" if session_states else "unknown",
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "result_document_count": len(result_links),
                "capability_gap": "public_executable_secondary_market_quotes_and_order_routing_not_available",
                "paper_only": True,
            },
        )


BangladeshBankAdapter = BangladeshBankTreasuryAuctionsAdapter
register_adapter(BangladeshBankTreasuryAuctionsAdapter())
