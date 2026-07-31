"""NZX official Global Dairy Trade event-reference adapter."""

from __future__ import annotations

import re

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, slug, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://www.nzx.com/markets/nzx-dairy-derivatives/global-dairy-trade/price-report"


def _price(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "–", "—", "n.s", "n.s."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_nzx_gdt(html: str) -> list[dict]:
    tables = html_tables(html)
    if not tables:
        return []
    summary = next((table for table in tables if table and table[0] and table[0][0] == "Products"), [])
    if not summary:
        return []
    event_id = str(summary[0][1]) if len(summary[0]) > 1 else "CURRENT"
    output: list[dict] = []
    for row in summary[1:]:
        if len(row) < 3:
            continue
        current = _price(row[1])
        previous = _price(row[2])
        if current is None:
            continue
        product = str(row[0]).strip()
        symbol = slug(product)
        change_pct = None
        if len(row) > 3:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", row[3])
            change_pct = float(match.group(0)) if match else None
        output.append(
            {
                "venue": "NZX_GDT",
                "inst_id": f"NZX_GDT:{symbol}",
                "instrument_id": f"NZX_GDT:{symbol}",
                "symbol": symbol,
                "name": product,
                "base": symbol,
                "quote": "USD_PER_TONNE",
                "market_type": "auction_reference",
                "market_surface": "global_dairy_trade_event_reference",
                "asset_class": "physical_dairy_auction",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": current,
                "previous": previous,
                "change_24h_pct": change_pct,
                "event_id": event_id,
                "data_status": "reachable",
                "quality_status": "event_reference",
                "session_status": "closed",
                "observed_at": utc_now(),
                "price_source": "NZX Global Dairy Trade price report",
                "source_url": SOURCE_URL,
                "candidate_reject_reason": "event_reference_requires_sgx_derivatives_route",
            }
        )
    return output


class NzxDairyAdapter:
    info = AdapterInfo(
        adapter_id="nzx_gdt_event_reference",
        venue="NZX_GDT",
        market_type="auction_reference",
        source="NZX official GDT price report",
        capabilities=("catalog", "event_price_reference", "settlement_reference"),
        aliases=("nzx dairy", "sgx-nzx dairy", "global dairy trade", "gdt"),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.nzx_dairy.NzxDairyAdapter",
        quote_assets=("USD_PER_TONNE",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get("nzx_gdt_event_reference", {})
        result = fetch_text(SOURCE_URL, int(cfg.get("timeout_seconds", 15)))
        observations = parse_nzx_gdt(result["text"]) if result["ok"] else []
        if not observations:
            observations = [health_observation("NZX_GDT", SOURCE_URL, result, "global_dairy_trade_event_reference")]
        return ScanBatch(
            source="NZX official GDT report",
            candidates=[],
            observations=observations,
            metadata={"adapter_id": self.info.adapter_id, "observation_count": len(observations), "source_status": result["status"]},
        )


register_adapter(NzxDairyAdapter())
