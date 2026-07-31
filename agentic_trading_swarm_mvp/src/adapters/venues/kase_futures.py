"""KASE official public futures-results reference adapter."""

from __future__ import annotations

import re

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, html_tables, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://kase.kz/en/markets/foreign-currency-futures"


def _decimal_comma(value: str) -> float | None:
    text = str(value or "").strip().replace(" ", "")
    if text in {"", "-", "–", "—"}:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_kase_futures(html: str) -> list[dict]:
    output: list[dict] = []
    for table in html_tables(html):
        for row in table:
            if len(row) < 10 or not re.match(r"^(US|RU)-\d", str(row[0])):
                continue
            symbol = str(row[0]).strip()
            settlement = _decimal_comma(row[1])
            last = _decimal_comma(row[4]) or settlement
            if last is None:
                continue
            bid = _decimal_comma(row[8])
            ask = _decimal_comma(row[9])
            output.append(
                {
                    "venue": "KASE",
                    "inst_id": f"KASE:{symbol}",
                    "instrument_id": f"KASE:{symbol}",
                    "symbol": symbol,
                    "base": "USD" if symbol.startswith("US-") else "RUB",
                    "quote": "KZT",
                    "market_type": "futures",
                    "market_surface": "kase_currency_futures_results",
                    "asset_class": "fx_futures",
                    "trade_type": "official_market_reference",
                    "direction": "watch_only",
                    "last": last,
                    "settlement_price": settlement,
                    "bid": bid,
                    "ask": ask,
                    "volume_local_millions": _decimal_comma(row[5]) or 0.0,
                    "transaction_count": int(_decimal_comma(row[6]) or 0),
                    "open_interest": _decimal_comma(row[7]) or 0.0,
                    "data_status": "reachable",
                    "quality_status": "reference_only",
                    "session_status": "unknown",
                    "observed_at": utc_now(),
                    "price_source": "KASE public futures results",
                    "source_url": SOURCE_URL,
                    "candidate_reject_reason": "settlement_or_delayed_reference_not_entry_quality",
                }
            )
    return output


class KaseFuturesAdapter:
    info = AdapterInfo(
        adapter_id="kase_futures_public_results",
        venue="KASE",
        market_type="futures",
        source="KASE official public market page",
        capabilities=("catalog", "settlement_reference", "delayed_quote", "session_reference"),
        aliases=("kazakhstan stock exchange", "kase", "kase futures"),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.kase_futures.KaseFuturesAdapter",
        quote_assets=("KZT",),
        default_cache_minutes=15,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get("kase_futures_public_results", {})
        result = fetch_text(SOURCE_URL, int(cfg.get("timeout_seconds", 15)))
        observations = parse_kase_futures(result["text"]) if result["ok"] else []
        if not observations:
            observations = [health_observation("KASE", SOURCE_URL, result, "kase_currency_futures_results")]
        return ScanBatch(
            source="KASE official public futures results",
            candidates=[],
            observations=observations,
            metadata={"adapter_id": self.info.adapter_id, "observation_count": len(observations), "source_status": result["status"]},
        )


register_adapter(KaseFuturesAdapter())
