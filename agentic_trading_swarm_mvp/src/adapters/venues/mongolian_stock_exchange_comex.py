"""Mongolian Stock Exchange Comex public auction and notice adapter.

Comex exposes a public dashboard snapshot for active and completed mineral
commodity auctions, and its article pages publish repricing and spot-contract
notices. The surface is useful for paper research, but this adapter never
claims executable routing or emits live-order candidates.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, number, slug, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://comex.mse.mn/show-article/1021"
DASHBOARD_URL = "https://comex.mse.mn/getdashboardtable"
HOME_URL = "https://comex.mse.mn"
MARKET_SURFACE = "mongolian_stock_exchange_comex_mining_product_auctions_and_notices"
VENUE = "MSE_COMEX"
MONGOLIA_TIME = dt.timezone(dt.timedelta(hours=8), name="Asia/Ulaanbaatar")


class MongolianStockExchangeComexParseError(ValueError):
    """Raised when the reachable public Comex surface changes shape."""


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
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - preserve parser drift as evidence.
        raise MongolianStockExchangeComexParseError(f"invalid Comex HTML: {exc}") from exc
    return " ".join(html.unescape(" ".join(parser.parts)).replace("\xa0", " ").split())


def _received_at(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or utc_now()).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MongolianStockExchangeComexParseError("received_at is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _parse_local_timestamp(value: str, field: str) -> dt.datetime:
    cleaned = " ".join(str(value or "").replace("\xa0", " ").split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(cleaned, fmt).replace(tzinfo=MONGOLIA_TIME)
        except ValueError:
            continue
    raise MongolianStockExchangeComexParseError(f"invalid {field}: {value!r}")


def _parse_dashboard_time(value: str) -> dt.datetime:
    return _parse_local_timestamp(value, "dashboard auction time")


def _currency_from_text(value: str) -> str:
    upper = str(value or "").upper()
    if "CNY" in upper or "ЮАН" in upper:
        return "CNY"
    if "USD" in upper or "АМ.ДОЛЛАР" in upper or "AM.DOLLAR" in upper:
        return "USD"
    if "MNT" in upper or "ТӨГРӨГ" in upper:
        return "MNT"
    return upper.strip() or "N/A"


def _quantity_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("\xa0", " ")
    if not text:
        return None
    match = re.search(r"([0-9][0-9,]*(?:\.\d+)?)", text)
    if not match:
        return None
    token = match.group(1)
    if "," in token and "." not in token:
        token = token.replace(",", "")
    return number(token)


def _parse_mongolian_datetime(text: str, field: str) -> dt.datetime:
    iso = re.search(r"\b(20\d{2})[./-](\d{2})[./-](\d{2}).{0,20}?(\d{1,2}):(\d{2})", text)
    if iso:
        year, month, day, hour, minute = (int(part) for part in iso.groups())
        return dt.datetime(year, month, day, hour, minute, tzinfo=MONGOLIA_TIME)
    local = re.search(
        r"(20\d{2})\s*оны\s*(\d{1,2})\s*(?:-?р|дугаар)?\s*сарын\s*(\d{1,2})"
        r"(?:-?ны|-?ний|-?нд|-?өдөр)?(?:\s*өдрийн)?\s*(\d{1,2}):(\d{2})\s*цаг",
        text,
        re.IGNORECASE,
    )
    if local:
        year, month, day, hour, minute = (int(part) for part in local.groups())
        return dt.datetime(year, month, day, hour, minute, tzinfo=MONGOLIA_TIME)
    raise MongolianStockExchangeComexParseError(f"could not parse {field}")


def _parse_mongolian_date(text: str, field: str) -> dt.date:
    match = re.search(
        r"(20\d{2})\s*оны\s*(\d{1,2})\s*(?:-?р|дугаар)?\s*сарын\s*(\d{1,2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise MongolianStockExchangeComexParseError(f"could not parse {field}")
    year, month, day = (int(part) for part in match.groups())
    return dt.date(year, month, day)


def _seller_from_text(text: str) -> str | None:
    match = re.search(
        r"[\"“]?([^\"”]+?(?:ХХК|ХК|ТӨҮГ|JSC|LLC))[\"”]?",
        str(text or ""),
        re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else None


def _freshness_from_event(
    event_at: dt.datetime,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> tuple[str, float]:
    age_seconds = max(0.0, (fetched_at - event_at.astimezone(dt.timezone.utc)).total_seconds())
    freshness = "fresh" if age_seconds <= max(0.0, stale_after_days) * 86400.0 else "stale"
    return freshness, round(age_seconds, 3)


def _dashboard_session_status(
    status: int,
    auction_at: dt.datetime,
    server_time: dt.datetime,
) -> str:
    comparable = server_time.astimezone(auction_at.tzinfo)
    if status == 3:
        return "completed"
    if status == 2:
        return "auction_live"
    if status == 1 and comparable >= auction_at:
        return "registration_open"
    return "scheduled"


def parse_mongolian_stock_exchange_comex_dashboard(
    payload: str | dict[str, Any],
    *,
    source_url: str = DASHBOARD_URL,
    received_at: str | None = None,
    stale_after_hours: float = 6.0,
) -> list[dict[str, Any]]:
    """Normalize the public Comex dashboard auction table."""

    if isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MongolianStockExchangeComexParseError(f"invalid dashboard JSON: {exc.msg}") from exc
    elif isinstance(payload, dict):
        document = payload
    else:
        raise MongolianStockExchangeComexParseError("dashboard payload must be a JSON object")

    rows = document.get("tableData")
    if not isinstance(rows, list):
        raise MongolianStockExchangeComexParseError("dashboard JSON is missing the tableData array")
    fetched_at = _received_at(received_at)
    server_time = _received_at(str(document.get("serverTime") or received_at or utc_now()))
    age_seconds = max(0.0, (fetched_at - server_time).total_seconds())
    freshness_state = (
        "fresh" if age_seconds <= max(0.0, stale_after_hours) * 3600.0 else "stale"
    )

    observations: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        auction_id = str(raw.get("auctionId") or "").strip()
        product_number = str(raw.get("productNumber") or "").strip()
        if not auction_id or not product_number:
            continue
        product_name = str(raw.get("productTypeNameEN") or raw.get("productTypeNameMN") or "").strip()
        seller_name = str(raw.get("sellerNameEN") or raw.get("sellerNameMN") or "").strip()
        currency = _currency_from_text(raw.get("currency"))
        starting_price = number(raw.get("productPrice"))
        deal_price = number(raw.get("orderPrice"))
        last = deal_price if deal_price is not None and deal_price > 0 else starting_price
        if last is None or last <= 0:
            continue
        auction_at = _parse_dashboard_time(str(raw.get("auctionStartTime") or ""))
        lot_count = int(number(raw.get("size")) or 0)
        tonnes_per_lot = number(raw.get("lot_price"))
        total_tonnage = round(lot_count * tonnes_per_lot, 6) if tonnes_per_lot else None
        status = int(number(raw.get("auctionStatus")) or 0)
        symbol = f"COMEX_{slug(product_name or product_number)}_{auction_id}"
        observations.append(
            {
                "venue": VENUE,
                "inst_id": f"{VENUE}:AUCTION:{auction_id}",
                "instrument_id": f"{VENUE}:AUCTION:{auction_id}",
                "symbol": symbol,
                "name": f"Comex {product_name or 'mining product'} auction {auction_id}",
                "base": slug(product_name or "mining_product"),
                "quote": currency,
                "market_type": "mineral_product_auction_snapshot",
                "market_surface": MARKET_SURFACE,
                "asset_class": "mineral_commodity",
                "trade_type": "official_auction_dashboard_snapshot",
                "direction": "watch_only",
                "last": last,
                "price_available": True,
                "price_basis": (
                    "published_deal_price_per_tonne"
                    if deal_price is not None and deal_price > 0
                    else "published_starting_price_per_tonne"
                ),
                "published_starting_price_per_tonne": starting_price,
                "published_deal_price_per_tonne": deal_price,
                "price_change_from_start_per_tonne": (
                    round(deal_price - starting_price, 6)
                    if deal_price is not None and starting_price is not None
                    else None
                ),
                "price_change_from_start_pct": (
                    round((deal_price - starting_price) / starting_price, 6)
                    if deal_price is not None and starting_price not in (None, 0)
                    else None
                ),
                "currency": currency,
                "product_number": int(product_number),
                "product_name_en": str(raw.get("productTypeNameEN") or "").strip() or None,
                "product_name_mn": str(raw.get("productTypeNameMN") or "").strip() or None,
                "seller_name_en": str(raw.get("sellerNameEN") or "").strip() or None,
                "seller_name_mn": str(raw.get("sellerNameMN") or "").strip() or None,
                "seller_name": seller_name or None,
                "auction_id": int(auction_id),
                "auction_status_code": status,
                "auction_at": auction_at.isoformat(),
                "lot_count": lot_count or None,
                "tonnes_per_lot": tonnes_per_lot,
                "total_tonnage": total_tonnage,
                "wet_size": number(raw.get("wetSize")),
                "data_access_type": "public_no_key",
                "data_status": "reachable",
                "fetch_status": "reachable",
                "quality_status": "official_dashboard_auction_snapshot",
                "freshness_state": freshness_state,
                "freshness_basis": "official_dashboard_server_time",
                "freshness_age_seconds": round(age_seconds, 3),
                "session_status": _dashboard_session_status(status, auction_at, server_time),
                "observed_at": server_time.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "price_source": "Mongolian Stock Exchange Comex public dashboard table",
                "source_url": source_url,
                "auction_detail_url": urljoin(HOME_URL, f"/multi/auctions/single/{auction_id}"),
                "paper_route_status": "synthetic_research_only",
                "execution_route_status": "route_needed",
                "candidate_reject_reason": "comex_auction_not_order_routable_from_public_dashboard",
            }
        )
    if not observations:
        raise MongolianStockExchangeComexParseError("dashboard table contained no usable auction rows")
    return observations


def parse_mongolian_stock_exchange_comex_notice_index(
    document: str,
    *,
    source_url: str = SOURCE_URL,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Return recent public Comex notice links from the article page sidebar."""

    if not isinstance(document, str) or not document.strip():
        raise MongolianStockExchangeComexParseError("notice index page is empty")
    if "show-article/" not in document:
        raise MongolianStockExchangeComexParseError("notice index page is missing article links")
    matches = re.finditer(
        r"<h6\b[^>]*>\s*<a\b[^>]*href=[\"']([^\"']*show-article/\d+)[\"'][^>]*>"
        r"(.*?)</a>\s*</h6>\s*<p\b[^>]*>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})</p>",
        document,
        re.IGNORECASE | re.DOTALL,
    )
    notices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        article_url = urljoin(source_url, html.unescape(match.group(1)).strip())
        if article_url in seen:
            continue
        title = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(match.group(2))).split())
        published_at = match.group(3).strip()
        if not title:
            continue
        notices.append(
            {
                "article_url": article_url,
                "title": title,
                "published_at": published_at,
                "article_id": article_url.rsplit("/", 1)[-1],
            }
        )
        seen.add(article_url)
    if not notices:
        raise MongolianStockExchangeComexParseError("notice index parser found no linked public notices")
    return notices[: max(1, int(limit))]


def _article_context(document: str) -> tuple[str, dt.datetime, str]:
    title_match = re.search(r"<h3\b[^>]*>(.*?)</h3>", document, re.IGNORECASE | re.DOTALL)
    date_match = re.search(
        r"<h6\b[^>]*>\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*</h6>",
        document,
        re.IGNORECASE | re.DOTALL,
    )
    article_match = re.search(r"<article\b[^>]*>(.*?)</article\s*>", document, re.IGNORECASE | re.DOTALL)
    if not title_match or not date_match or not article_match:
        raise MongolianStockExchangeComexParseError("notice page is missing title, published timestamp, or article body")
    title = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(title_match.group(1))).split())
    published_at = _parse_local_timestamp(date_match.group(1), "notice published timestamp")
    article_html = article_match.group(1)
    return title, published_at, article_html


def _parse_price_change_rows(
    *,
    article_id: str,
    title: str,
    article_html: str,
    source_url: str,
    published_at: dt.datetime,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> list[dict[str, Any]]:
    tables = html_tables(article_html)
    seller_name = _seller_from_text(title)
    rows: list[dict[str, Any]] = []
    for table in tables:
        if not table:
            continue
        headers = [" ".join(cell.lower().split()) for cell in table[0]]
        if len(headers) < 5 or not any("хуучин" in header for header in headers) or not any(
            "шинэчлэгдсэн" in header or "шинэ" in header for header in headers
        ):
            continue
        for index, line in enumerate(table[1:], start=1):
            if len(line) < 5:
                continue
            schedule_text, product_text, quantity_text, old_text, new_text = line[:5]
            new_price = number(new_text)
            old_price = number(old_text)
            if new_price is None or old_price is None or new_price <= 0 or old_price <= 0:
                continue
            auction_at = _parse_mongolian_datetime(schedule_text, "price-change schedule")
            freshness_state, freshness_age = _freshness_from_event(auction_at, fetched_at, stale_after_days)
            currency = _currency_from_text(f"{old_text} {new_text}")
            product_name = " ".join(product_text.split())
            rows.append(
                {
                    "venue": VENUE,
                    "inst_id": f"{VENUE}:NOTICE:{article_id}:PRICE_CHANGE:{index}",
                    "instrument_id": f"{VENUE}:NOTICE:{article_id}:PRICE_CHANGE:{index}",
                    "symbol": f"COMEX_NOTICE_{article_id}_{index}",
                    "name": f"Comex notice price change {article_id} row {index}",
                    "base": slug(product_name or "mineral_product"),
                    "quote": currency,
                    "market_type": "mineral_product_price_change_notice",
                    "market_surface": MARKET_SURFACE,
                    "asset_class": "mineral_commodity",
                    "trade_type": "official_auction_price_change_notice",
                    "direction": "watch_only",
                    "last": new_price,
                    "price_available": True,
                    "price_basis": "published_updated_auction_price_per_tonne",
                    "published_old_price_per_tonne": old_price,
                    "published_new_price_per_tonne": new_price,
                    "published_price_delta_per_tonne": round(new_price - old_price, 6),
                    "published_price_delta_pct": round((new_price - old_price) / old_price, 6),
                    "currency": currency,
                    "seller_name": seller_name,
                    "notice_title": title,
                    "notice_article_id": int(article_id),
                    "product_name": product_name,
                    "scheduled_auction_at": auction_at.isoformat(),
                    "total_tonnage": _quantity_number(quantity_text),
                    "quantity_text": " ".join(quantity_text.split()) or None,
                    "old_condition_text": " ".join(old_text.split()),
                    "new_condition_text": " ".join(new_text.split()),
                    "data_access_type": "public_no_key",
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_auction_price_change_notice",
                    "freshness_state": freshness_state,
                    "freshness_basis": "official_scheduled_auction_time",
                    "freshness_age_seconds": freshness_age,
                    "session_status": "scheduled"
                    if fetched_at.astimezone(MONGOLIA_TIME) < auction_at
                    else "completed",
                    "observed_at": published_at.isoformat(),
                    "fetched_at": fetched_at.isoformat(),
                    "price_source": "Mongolian Stock Exchange Comex price-change notice",
                    "source_url": source_url,
                    "paper_route_status": "synthetic_research_only",
                    "execution_route_status": "route_needed",
                    "candidate_reject_reason": "comex_notice_not_order_routable",
                }
            )
    if not rows:
        raise MongolianStockExchangeComexParseError("price-change notice table contained no usable rows")
    return rows


def _spot_notice_row(
    *,
    article_id: str,
    title: str,
    article_text: str,
    source_url: str,
    published_at: dt.datetime,
    fetched_at: dt.datetime,
    stale_after_days: float,
) -> dict[str, Any]:
    auction_at = _parse_mongolian_datetime(article_text, "spot-contract auction time")
    price_match = re.search(
        r"([0-9][0-9,]*(?:\.\d+)?)\s*(юань|yuan|CNY|USD|ам\.?доллар)",
        article_text,
        re.IGNORECASE,
    )
    if not price_match:
        raise MongolianStockExchangeComexParseError("spot-contract notice is missing a published starting price")
    starting_price = number(price_match.group(1))
    if starting_price is None or starting_price <= 0:
        raise MongolianStockExchangeComexParseError("spot-contract notice starting price is invalid")
    tonnage_match = re.search(r"([0-9][0-9,]*)\s*тонн", article_text, re.IGNORECASE)
    if not tonnage_match:
        raise MongolianStockExchangeComexParseError("spot-contract notice is missing total tonnage")
    total_tonnage = _quantity_number(tonnage_match.group(1))
    lot_match = re.search(r"(\d+)\s*багц", article_text, re.IGNORECASE)
    delivery_date = _parse_mongolian_date(article_text, "spot-contract delivery date")
    freshness_state, freshness_age = _freshness_from_event(auction_at, fetched_at, stale_after_days)
    product_name = "1/3 coking coal" if "1/3" in article_text else "coal"
    route_match = re.search(r"(Шивээхүрэн[^.]{0,80}?Сэхэ\s*боомт)", article_text, re.IGNORECASE)
    return {
        "venue": VENUE,
        "inst_id": f"{VENUE}:NOTICE:{article_id}:SPOT_CONTRACT",
        "instrument_id": f"{VENUE}:NOTICE:{article_id}:SPOT_CONTRACT",
        "symbol": f"COMEX_SPOT_{article_id}",
        "name": f"Comex spot-contract notice {article_id}",
        "base": "COAL_1_3_COKING" if "1/3" in article_text else "COAL",
        "quote": _currency_from_text(price_match.group(2)),
        "market_type": "mineral_spot_contract_notice",
        "market_surface": MARKET_SURFACE,
        "asset_class": "mineral_commodity",
        "trade_type": "official_spot_contract_notice",
        "direction": "watch_only",
        "last": starting_price,
        "price_available": True,
        "price_basis": "published_spot_contract_starting_price_per_tonne",
        "published_starting_price_per_tonne": starting_price,
        "currency": _currency_from_text(price_match.group(2)),
        "seller_name": _seller_from_text(title) or _seller_from_text(article_text),
        "product_name": product_name,
        "notice_title": title,
        "notice_article_id": int(article_id),
        "auction_at": auction_at.isoformat(),
        "delivery_deadline": dt.datetime.combine(delivery_date, dt.time.min, tzinfo=MONGOLIA_TIME).isoformat(),
        "lot_count": int(lot_match.group(1)) if lot_match else None,
        "total_tonnage": total_tonnage,
        "delivery_route": " ".join(route_match.group(1).split()) if route_match else None,
        "contract_type": "spot",
        "data_access_type": "public_no_key",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_spot_contract_notice",
        "freshness_state": freshness_state,
        "freshness_basis": "official_spot_auction_time",
        "freshness_age_seconds": freshness_age,
        "session_status": "scheduled" if fetched_at.astimezone(MONGOLIA_TIME) < auction_at else "completed",
        "observed_at": published_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Mongolian Stock Exchange Comex spot-contract notice",
        "source_url": source_url,
        "paper_route_status": "synthetic_research_only",
        "execution_route_status": "route_needed",
        "candidate_reject_reason": "comex_spot_contract_notice_not_order_routable",
    }


def _reschedule_notice_row(
    *,
    article_id: str,
    title: str,
    article_text: str,
    source_url: str,
    published_at: dt.datetime,
    fetched_at: dt.datetime,
) -> dict[str, Any]:
    matches = re.findall(
        r"(20\d{2}\s*оны\s*\d{1,2}\s*(?:-?р|дугаар)?\s*сарын\s*\d{1,2}"
        r"(?:-?ны|-?ний|-?нд|-?өдрийн)?(?:\s*өдрийн)?\s*\d{1,2}:\d{2}\s*цаг)",
        article_text,
        re.IGNORECASE,
    )
    if len(matches) < 2:
        raise MongolianStockExchangeComexParseError("reschedule notice is missing the cancelled and rescheduled times")
    cancelled_at = _parse_mongolian_datetime(matches[0], "cancelled auction time")
    rescheduled_at = _parse_mongolian_datetime(matches[1], "rescheduled auction time")
    tonnage_match = re.search(r"([0-9][0-9,]*)\s*тонн", article_text, re.IGNORECASE)
    lot_match = re.search(r"(\d+)\s*багц", article_text, re.IGNORECASE)
    product_match = re.search(r"([0-9]/[0-9]\s*коксжих\s*нүүрс)", article_text, re.IGNORECASE)
    return {
        "venue": VENUE,
        "inst_id": f"{VENUE}:NOTICE:{article_id}:RESCHEDULE",
        "instrument_id": f"{VENUE}:NOTICE:{article_id}:RESCHEDULE",
        "symbol": f"COMEX_RESCHEDULE_{article_id}",
        "name": f"Comex auction reschedule notice {article_id}",
        "base": "COAL" if "нүүрс" in article_text.lower() else "MINERAL_PRODUCT",
        "quote": "N/A",
        "market_type": "mineral_auction_reschedule_notice",
        "market_surface": MARKET_SURFACE,
        "asset_class": "mineral_commodity",
        "trade_type": "official_auction_reschedule_notice",
        "direction": "watch_only",
        "last": 0.0,
        "price_available": False,
        "seller_name": _seller_from_text(article_text) or _seller_from_text(title),
        "product_name": " ".join(product_match.group(1).split()) if product_match else None,
        "notice_title": title,
        "notice_article_id": int(article_id),
        "cancelled_auction_at": cancelled_at.isoformat(),
        "rescheduled_auction_at": rescheduled_at.isoformat(),
        "lot_count": int(lot_match.group(1)) if lot_match else None,
        "total_tonnage": _quantity_number(tonnage_match.group(1)) if tonnage_match else None,
        "data_access_type": "public_no_key",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_auction_reschedule_notice",
        "freshness_state": "fresh",
        "freshness_basis": "official_notice_publication",
        "freshness_age_seconds": max(0.0, round((fetched_at - published_at.astimezone(dt.timezone.utc)).total_seconds(), 3)),
        "session_status": "rescheduled",
        "observed_at": published_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Mongolian Stock Exchange Comex reschedule notice",
        "source_url": source_url,
        "paper_route_status": "synthetic_research_only",
        "execution_route_status": "route_needed",
        "candidate_reject_reason": "comex_reschedule_notice_not_order_routable",
    }


def _generic_notice_row(
    *,
    article_id: str,
    title: str,
    article_text: str,
    source_url: str,
    published_at: dt.datetime,
    fetched_at: dt.datetime,
) -> dict[str, Any]:
    return {
        "venue": VENUE,
        "inst_id": f"{VENUE}:NOTICE:{article_id}:REFERENCE",
        "instrument_id": f"{VENUE}:NOTICE:{article_id}:REFERENCE",
        "symbol": f"COMEX_NOTICE_{article_id}",
        "name": f"Comex public notice {article_id}",
        "base": "MONGOLIA_MINING_PRODUCT",
        "quote": "N/A",
        "market_type": "mineral_market_notice_reference",
        "market_surface": MARKET_SURFACE,
        "asset_class": "mineral_market_operations",
        "trade_type": "official_market_notice_reference",
        "direction": "watch_only",
        "last": 0.0,
        "price_available": False,
        "seller_name": _seller_from_text(title) or _seller_from_text(article_text),
        "notice_title": title,
        "notice_article_id": int(article_id),
        "notice_excerpt": article_text[:400],
        "data_access_type": "public_no_key",
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_market_notice_reference",
        "freshness_state": "fresh",
        "freshness_basis": "official_notice_publication",
        "freshness_age_seconds": max(0.0, round((fetched_at - published_at.astimezone(dt.timezone.utc)).total_seconds(), 3)),
        "session_status": "reference_only",
        "observed_at": published_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "price_source": "Mongolian Stock Exchange Comex public notice",
        "source_url": source_url,
        "paper_route_status": "synthetic_research_only",
        "execution_route_status": "route_needed",
        "candidate_reject_reason": "comex_notice_reference_not_order_routable",
    }


def parse_mongolian_stock_exchange_comex_notice_page(
    document: str,
    *,
    source_url: str,
    received_at: str | None = None,
    stale_after_days: float = 90.0,
) -> list[dict[str, Any]]:
    """Normalize a public Comex notice page into research observations."""

    if not isinstance(document, str) or not document.strip():
        raise MongolianStockExchangeComexParseError("notice page is empty")
    title, published_at, article_html = _article_context(document)
    article_id = re.search(r"(\d+)$", source_url)
    if not article_id:
        raise MongolianStockExchangeComexParseError("notice source URL is missing the article id")
    fetched_at = _received_at(received_at)
    article_text = _visible_text(article_html)
    lowered = f"{title} {article_text}".casefold()
    article_key = article_id.group(1)
    if "хуучин нөхцөл" in lowered and ("шинэчлэгдсэн нөхцөл" in lowered or "шинэ нөхцөл" in lowered):
        return _parse_price_change_rows(
            article_id=article_key,
            title=title,
            article_html=article_html,
            source_url=source_url,
            published_at=published_at,
            fetched_at=fetched_at,
            stale_after_days=stale_after_days,
        )
    if "спот" in lowered and "эхлэх үнэ" in lowered:
        return [
            _spot_notice_row(
                article_id=article_key,
                title=title,
                article_text=article_text,
                source_url=source_url,
                published_at=published_at,
                fetched_at=fetched_at,
                stale_after_days=stale_after_days,
            )
        ]
    if "цуцалж" in lowered and "дахин зохион байгуулах" in lowered:
        return [
            _reschedule_notice_row(
                article_id=article_key,
                title=title,
                article_text=article_text,
                source_url=source_url,
                published_at=published_at,
                fetched_at=fetched_at,
            )
        ]
    return [
        _generic_notice_row(
            article_id=article_key,
            title=title,
            article_text=article_text,
            source_url=source_url,
            published_at=published_at,
            fetched_at=fetched_at,
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
    source_key: str,
    source_url: str,
    result: dict[str, Any],
    parser_error: str | None = None,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    row = health_observation(VENUE, source_url, evidence, MARKET_SURFACE)
    row.update(
        {
            "inst_id": f"{VENUE}:HEALTH:{slug(source_key)}",
            "instrument_id": f"{VENUE}:HEALTH:{slug(source_key)}",
            "symbol": f"{slug(source_key)}_HEALTH",
            "fetch_status": str(result.get("status") or "unavailable"),
            "quality_status": "source_health",
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "source_unavailable",
            "freshness_age_seconds": None,
            "session_status": "unknown",
            "parser_failure": parser_error,
            "paper_route_status": "synthetic_research_only",
            "execution_route_status": "route_needed",
            "candidate_reject_reason": (
                "public_mse_comex_parser_failure"
                if parser_error
                else "public_mse_comex_source_unavailable"
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


class MongolianStockExchangeComexAdapter:
    info = AdapterInfo(
        adapter_id="mongolian_stock_exchange_comex",
        venue=VENUE,
        market_type="mineral_product_auction_snapshot",
        source="Mongolian Stock Exchange Comex public mining-product auction dashboard and notices",
        capabilities=(
            "public_market_data",
            "auction_schedule",
            "auction_price_change",
            "spot_contract_notice",
            "event_price_reference",
            "delayed_quote",
            "source_health",
        ),
        aliases=(
            "mongolian stock exchange comex",
            "mongolian stock exchange",
            "mse comex",
            "mongolia mining product exchange",
            "comex mse mn",
            "mongolia coal auction",
        ),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.mongolian_stock_exchange_comex.MongolianStockExchangeComexAdapter",
        quote_assets=("USD", "CNY", "MNT"),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 15)))
        stale_after_hours = max(0.0, float(cfg.get("dashboard_stale_after_hours", 6.0)))
        stale_after_days = max(0.0, float(cfg.get("notice_stale_after_days", 90.0)))
        max_notices = max(1, min(int(cfg.get("max_notice_documents", 6)), 12))
        dashboard_url = str(cfg.get("dashboard_url") or DASHBOARD_URL)
        index_url = str(cfg.get("source_url") or SOURCE_URL)
        observations: list[dict[str, Any]] = []
        parser_failures: list[dict[str, str]] = []
        fetch_status: dict[str, dict[str, Any]] = {}
        usable_sources = 0
        source_urls = [dashboard_url, index_url]

        dashboard = fetch_text(dashboard_url, timeout)
        fetch_status["dashboard_table"] = _fetch_evidence(dashboard, dashboard_url)
        if not dashboard.get("ok"):
            observations.append(_failure_observation("dashboard_table", dashboard_url, dashboard))
        else:
            try:
                observations.extend(
                    parse_mongolian_stock_exchange_comex_dashboard(
                        str(dashboard.get("text") or ""),
                        source_url=dashboard_url,
                        received_at=dashboard.get("received_at"),
                        stale_after_hours=stale_after_hours,
                    )
                )
                usable_sources += 1
            except (MongolianStockExchangeComexParseError, TypeError, ValueError) as exc:
                message = f"Comex dashboard parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source_key": "dashboard_table", "source_url": dashboard_url, "error": message}
                )
                observations.append(_failure_observation("dashboard_table", dashboard_url, dashboard, message))

        index = fetch_text(index_url, timeout)
        fetch_status["notice_index"] = _fetch_evidence(index, index_url)
        notice_links: list[dict[str, Any]] = []
        if not index.get("ok"):
            observations.append(_failure_observation("notice_index", index_url, index))
        else:
            try:
                notice_links = parse_mongolian_stock_exchange_comex_notice_index(
                    str(index.get("text") or ""),
                    source_url=index_url,
                    limit=max_notices,
                )
                usable_sources += 1
                source_urls.extend(item["article_url"] for item in notice_links)
            except (MongolianStockExchangeComexParseError, TypeError, ValueError) as exc:
                message = f"Comex notice-index parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source_key": "notice_index", "source_url": index_url, "error": message}
                )
                observations.append(_failure_observation("notice_index", index_url, index, message))

        for sequence, notice in enumerate(notice_links, start=1):
            article_url = str(notice["article_url"])
            source_key = f"notice_{sequence}"
            result = fetch_text(article_url, timeout)
            fetch_status[source_key] = _fetch_evidence(result, article_url)
            if not result.get("ok"):
                observations.append(_failure_observation(source_key, article_url, result))
                continue
            try:
                observations.extend(
                    parse_mongolian_stock_exchange_comex_notice_page(
                        str(result.get("text") or ""),
                        source_url=article_url,
                        received_at=result.get("received_at"),
                        stale_after_days=stale_after_days,
                    )
                )
                usable_sources += 1
            except (MongolianStockExchangeComexParseError, TypeError, ValueError) as exc:
                message = f"Comex notice parser failed: {exc}"[:300]
                parser_failures.append(
                    {"source_key": source_key, "source_url": article_url, "error": message}
                )
                observations.append(_failure_observation(source_key, article_url, result, message))

        statuses = [item["fetch_status"] for item in fetch_status.values()]
        if usable_sources == len(fetch_status) and not parser_failures:
            source_status = "reachable"
        elif usable_sources or parser_failures:
            source_status = "degraded"
        elif "blocked" in statuses:
            source_status = "blocked"
        else:
            source_status = "unavailable"

        real_rows = [row for row in observations if row.get("quality_status") != "source_health"]
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in real_rows})
        session_states = sorted({str(row.get("session_status") or "unknown") for row in real_rows})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 402,
                "source_status": source_status,
                "source_url": index_url,
                "source_urls": source_urls,
                "fetch_status": fetch_status,
                "freshness_state": (
                    freshness_states[0]
                    if len(freshness_states) == 1
                    else "mixed"
                    if freshness_states
                    else "unknown"
                ),
                "freshness_states": freshness_states,
                "session_state": (
                    session_states[0]
                    if len(session_states) == 1
                    else "mixed"
                    if session_states
                    else "unknown"
                ),
                "session_states": session_states,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": len(real_rows),
                "notice_document_count": len(notice_links),
                "capability_gap": "public_order_book_member_identity_and_order_routing_not_available",
                "paper_only": True,
            },
        )


MongolianComexAdapter = MongolianStockExchangeComexAdapter
register_adapter(MongolianStockExchangeComexAdapter())
