"""TWSE official no-key daily cash-equity reference adapter."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, number, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


def _roc_date(value: str) -> str | None:
    text = str(value or "").strip()
    if len(text) != 7 or not text.isdigit():
        return None
    try:
        return dt.date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7])).isoformat()
    except ValueError:
        return None


def parse_twse_daily(payload: Any, limit: int = 300) -> list[dict]:
    rows = payload if isinstance(payload, list) else []
    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("Code") or "").strip()
        last = number(row.get("ClosingPrice"))
        if not code or last is None or last <= 0:
            continue
        trade_value = number(row.get("TradeValue")) or 0.0
        observed_date = _roc_date(str(row.get("Date") or ""))
        parsed.append(
            {
                "venue": "TWSE",
                "inst_id": f"TWSE:{code}",
                "instrument_id": f"TWSE:{code}",
                "symbol": code,
                "name": str(row.get("Name") or code),
                "base": code,
                "quote": "TWD",
                "market_type": "equity",
                "market_surface": "twse_cash_equity_daily",
                "asset_class": "local_equity",
                "trade_type": "official_market_reference",
                "direction": "watch_only",
                "last": last,
                "open": number(row.get("OpeningPrice")),
                "high": number(row.get("HighestPrice")),
                "low": number(row.get("LowestPrice")),
                "change": number(row.get("Change")),
                "base_volume_24h": number(row.get("TradeVolume")) or 0.0,
                "local_quote_volume_24h": trade_value,
                "data_status": "reachable",
                "quality_status": "reference_only",
                "session_status": "closed",
                "observed_at": f"{observed_date}T05:30:00+00:00" if observed_date else utc_now(),
                "price_source": "TWSE OpenAPI STOCK_DAY_ALL",
                "source_url": SOURCE_URL,
                "candidate_reject_reason": "daily_reference_not_entry_quality",
            }
        )
    parsed.sort(key=lambda item: float(item.get("local_quote_volume_24h") or 0.0), reverse=True)
    return parsed[: max(1, int(limit))]


class TwseDailyAdapter:
    info = AdapterInfo(
        adapter_id="twse_daily_public",
        venue="TWSE",
        market_type="equity",
        source="TWSE official OpenAPI",
        capabilities=("catalog", "daily_ohlcv", "ticker_reference", "session_reference"),
        aliases=("taiwan stock exchange", "twse", "taiwan equities"),
        docs_url="https://openapi.twse.com.tw/",
        runtime_entrypoint="adapters.venues.twse_daily.TwseDailyAdapter",
        quote_assets=("TWD",),
        default_cache_minutes=60,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get("twse_daily_public", {})
        result = fetch_text(SOURCE_URL, int(cfg.get("timeout_seconds", 15)))
        observations = []
        if result["ok"]:
            try:
                observations = parse_twse_daily(json.loads(result["text"]), int(cfg.get("max_instruments", 300)))
            except (ValueError, TypeError) as exc:
                result = {**result, "status": "degraded", "error": f"TWSE parser failed: {exc}"}
        if not observations:
            observations = [health_observation("TWSE", SOURCE_URL, result, "twse_cash_equity_daily")]
        return ScanBatch(
            source="TWSE official OpenAPI",
            candidates=[],
            observations=observations,
            metadata={"adapter_id": self.info.adapter_id, "observation_count": len(observations), "source_status": result["status"]},
        )


register_adapter(TwseDailyAdapter())
