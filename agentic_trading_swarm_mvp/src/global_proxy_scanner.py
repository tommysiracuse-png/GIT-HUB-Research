#!/usr/bin/env python3
"""Global ETF/ADR proxy scanner using Yahoo chart data.

This scanner gives the radar immediate international market coverage without
API keys. It is a discovery and paper-trading adapter, not institutional-grade
market data.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import pathlib
import statistics
import sys
import time
import urllib.parse
import urllib.request

from scan_batch import ScanBatch, observation_from_candidate
from paper_context_cost import annotate_paper_context_cost
from yahoo_proxy_reuse import evaluate_yahoo_proxy_reuse


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
UNIVERSE_PATH = ROOT / "config" / "global_market_universe.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def load_universe(path: pathlib.Path = UNIVERSE_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_chart(symbol: str, range_: str = "5d", interval: str = "15m", timeout: int = 12) -> dict:
    params = urllib.parse.urlencode({"range": range_, "interval": interval})
    url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 inefficiency-radar/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo chart returned no result for {symbol}")
    return result[0]


def valid_pairs(values: list[float | None], volumes: list[float | None]) -> list[tuple[float, float]]:
    pairs = []
    for price, volume in zip(values, volumes):
        if price is None:
            continue
        pairs.append((float(price), float(volume or 0.0)))
    return pairs


def bps_change(new: float, old: float) -> float:
    if old <= 0:
        return 0.0
    return (new / old - 1.0) * 10_000.0


def liquidity_score(dollar_volume: float) -> float:
    if dollar_volume <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(dollar_volume) - 5.0) / 3.5))


def estimated_spread_bps(score: float) -> float:
    if score >= 0.85:
        return 3.0
    if score >= 0.65:
        return 6.0
    if score >= 0.45:
        return 10.0
    return 18.0


def feasibility(direction: str, settings: dict) -> dict:
    caps = settings.get("account_capabilities", {})
    if direction == "long_proxy":
        status = "standard" if caps.get("equity_long", True) else "conditional"
        return {
            "status": status,
            "requires_short_spot": False,
            "legs": ["buy US-listed ETF/ADR proxy"],
            "notes": ["Long US-listed ETF/ADR exposure is generally feasible with a standard equity brokerage account."],
        }
    if direction == "short_proxy":
        allowed = caps.get("equity_short", False) or caps.get("options", False)
        return {
            "status": "standard" if allowed else "conditional",
            "requires_short_spot": False,
            "legs": ["short ETF/ADR or buy put/spread if available"],
            "notes": [
                "Short exposure requires stock borrow, margin approval, inverse ETF, or options approval.",
                "Without those permissions, negative signals remain research-only.",
            ],
        }
    return {
        "status": "watch_only",
        "requires_short_spot": False,
        "legs": [],
        "notes": ["No executable direction."],
    }


def build_candidate(item: dict, settings: dict) -> dict | None:
    chart = fetch_chart(item["symbol"])
    decision_time = dt.datetime.now(dt.timezone.utc)
    reuse_gate = evaluate_yahoo_proxy_reuse(chart, settings, now=decision_time)
    meta = chart.get("meta", {})
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    timestamps = chart.get("timestamp") or []
    pairs = valid_pairs(closes, volumes)
    if len(pairs) < 12:
        return None

    last = pairs[-1][0]
    ref_1d = pairs[-27][0] if len(pairs) >= 27 else pairs[0][0]
    ref_short = pairs[-5][0] if len(pairs) >= 5 else pairs[0][0]
    ret_1d_bps = bps_change(last, ref_1d)
    ret_short_bps = bps_change(last, ref_short)
    recent_dollar_volume = sum(price * volume for price, volume in pairs[-27:])
    liq = liquidity_score(recent_dollar_volume)
    spread = estimated_spread_bps(liq)
    last_ts = timestamps[-1] if timestamps else int(time.time())
    last_seen = dt.datetime.fromtimestamp(last_ts, tz=dt.timezone.utc)
    stale_minutes = (decision_time - last_seen).total_seconds() / 60.0

    direction = "long_proxy" if ret_1d_bps >= 0 else "short_proxy"
    abs_signal = abs(ret_1d_bps) * 0.11 + abs(ret_short_bps) * 0.16
    gross_edge_bps = min(abs_signal, 45.0)
    one_bar_returns = [
        bps_change(pairs[index][0], pairs[index - 1][0])
        for index in range(max(1, len(pairs) - 12), len(pairs))
    ]
    recent_volatility_bps = statistics.pstdev(one_bar_returns) if len(one_bar_returns) > 1 else 0.0
    edge_bps = round(max(0.0, gross_edge_bps - spread), 3)
    score = round(max(0.0, min(100.0, abs_signal + (liq * 25.0) - spread - min(stale_minutes / 10.0, 15.0))), 3)
    if stale_minutes > 120:
        direction = "watch_only"
        score = min(score, 25.0)

    candidate = {
        "seen_at": decision_time.isoformat(),
        "venue": "YAHOO_PROXY",
        "inst_id": item["symbol"],
        "name": item["name"],
        "region": item["region"],
        "asset_class": item["asset_class"],
        "trade_type": "global_proxy_momentum",
        "direction": direction,
        "execution_feasibility": feasibility(direction, settings),
        "thesis": "international ETF/ADR proxy short-term momentum and shock detection",
        "last": round(last, 6),
        "funding_bps": 0.0,
        "basis_bps": 0.0,
        "gross_edge_bps_estimate": round(gross_edge_bps, 3),
        "edge_bps_estimate": edge_bps,
        "change_24h_pct": round(ret_1d_bps / 100.0, 3),
        "short_return_pct": round(ret_short_bps / 100.0, 3),
        "quote_volume_24h": round(recent_dollar_volume, 2),
        "liquidity_score": round(liq, 3),
        "spread_bps": round(spread, 3),
        "recent_volatility_bps": round(recent_volatility_bps, 3),
        "last_bar_utc": last_seen.isoformat(),
        "source_bar_end_utc": last_seen.isoformat(),
        "decision_time_utc": decision_time.isoformat(),
        "provider_age_seconds": round(max(0.0, (decision_time - last_seen).total_seconds()), 3),
        "entry_price_convention": "decision_time_last_bar_price",
        "stale_minutes": round(stale_minutes, 1),
        "proxy_valid_for_reuse": reuse_gate["proxy_valid_for_reuse"],
        "proxy_reuse_gate": reuse_gate,
        "score": score,
        "risk_notes": [
            "paper-trade only",
            "Yahoo chart data is suitable for discovery, not production execution",
            "US-listed proxy may not perfectly track local market after foreign close",
            "short exposure requires borrow, margin, inverse product, or options",
        ],
        "data_source": {
            "provider": "Yahoo Finance chart endpoint",
            "symbol": meta.get("symbol", item["symbol"]),
            "exchange": meta.get("exchangeName"),
            "currency": meta.get("currency"),
        },
    }
    return annotate_paper_context_cost(candidate, settings)


def build_scan_batch(
    settings: dict,
    limit: int | None = None,
    required_inst_ids: set[str] | None = None,
) -> ScanBatch:
    universe = load_universe()
    known = {item["symbol"] for item in universe}
    for symbol in sorted(required_inst_ids or set()):
        if symbol not in known:
            universe.append(
                {
                    "symbol": symbol,
                    "name": f"Open paper instrument {symbol}",
                    "region": "unknown",
                    "asset_class": "equity_proxy",
                }
            )
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(build_candidate, item, settings): item for item in universe}
        for future in concurrent.futures.as_completed(futures):
            try:
                candidate = future.result()
            except Exception:
                candidate = None
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda row: row["score"], reverse=True)
    observations = [
        observation_from_candidate(candidate, source="Yahoo Finance chart endpoint")
        for candidate in candidates
    ]
    selected = candidates[:limit] if limit else candidates
    return ScanBatch(
        source="Yahoo Finance chart endpoint",
        candidates=selected,
        observations=observations,
        metadata={"priced_instrument_count": len(observations)},
    )


def build_candidates(settings: dict, limit: int | None = None) -> list[dict]:
    return build_scan_batch(settings, limit=limit).candidates


def write_outputs(candidates: list[dict]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "global_proxy_latest.json"
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "Yahoo Finance chart endpoint",
        "mode": "research_paper_only",
        "candidates": candidates,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def print_table(candidates: list[dict], top: int) -> None:
    print("Rank | Score | Symbol | Dir | Region | 1d % | Short % | Liq | Spread | Stale")
    print("-" * 94)
    for idx, row in enumerate(candidates[:top], start=1):
        print(
            f"{idx:>4} | {row['score']:>5.1f} | {row['inst_id']:<6} | "
            f"{row['direction']:<10} | {row['region']:<15} | "
            f"{row['change_24h_pct']:>6.2f} | {row['short_return_pct']:>7.2f} | "
            f"{row['liquidity_score']:>4.2f} | {row['spread_bps']:>6.1f} | {row['stale_minutes']:>5.1f}m"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan global ETF/ADR proxy markets.")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args(argv)

    from settings import load_settings

    settings = load_settings()
    candidates = build_candidates(settings)
    path = write_outputs(candidates)
    print_table(candidates, args.top)
    print()
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
