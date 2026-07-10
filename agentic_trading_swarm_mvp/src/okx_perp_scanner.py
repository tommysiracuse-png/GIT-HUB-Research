#!/usr/bin/env python3
"""OKX perpetual swap opportunity scanner.

This is a fast, dependency-free market sensor for the agent swarm MVP.
It writes ranked short-term dislocation candidates to JSON.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import collections
import datetime as dt
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from scan_batch import ScanBatch


BASE_URL = "https://www.okx.com"
ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"


def fetch_json(path: str, params: dict[str, str] | None = None, timeout: int = 12) -> dict:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        BASE_URL + path + query,
        headers={"User-Agent": "inefficiency-radar/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error for {path}: {data}")
    return data


def as_float(value: str | None, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def bps(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator - 1.0) * 10_000.0


def unix_ms_to_iso(value: str | int | None) -> str | None:
    if not value:
        return None
    try:
        stamp = int(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).isoformat()


def liquidity_score(quote_volume: float) -> float:
    if quote_volume <= 0:
        return 0.0
    # Smoothly maps roughly $100k -> low, $10m+ -> high.
    return max(0.0, min(1.0, (math.log10(quote_volume) - 5.0) / 3.0))


def classify_direction(funding_bps: float, basis_bps: float) -> tuple[str, str]:
    if funding_bps > 0 and basis_bps > 0:
        return "short_perp_long_spot", "positive funding plus positive perp/index basis"
    if funding_bps < 0 and basis_bps < 0:
        return "long_perp_short_spot", "negative funding plus negative perp/index basis"
    if abs(basis_bps) > 25:
        direction = "basis_mean_reversion_short_perp" if basis_bps > 0 else "basis_mean_reversion_long_perp"
        return direction, "large perp/index basis without funding confirmation"
    if abs(funding_bps) > 3:
        direction = "funding_capture_short_perp" if funding_bps > 0 else "funding_capture_long_perp"
        return direction, "large funding rate without basis confirmation"
    return "watch_only", "weak or mixed signal"


def execution_feasibility(direction: str, allow_short_spot: bool) -> dict:
    """Describe whether the trade can be executed without hard-to-source legs."""
    if direction in {"short_perp_long_spot", "funding_capture_short_perp", "basis_mean_reversion_short_perp"}:
        return {
            "status": "standard",
            "requires_short_spot": False,
            "legs": ["short perpetual", "optionally buy spot/index hedge"],
            "notes": [
                "Perp shorting is generally supported on derivatives venues.",
                "Long spot hedge is operationally simple if the asset is listed and liquid.",
            ],
        }
    if direction in {"long_perp_short_spot", "funding_capture_long_perp", "basis_mean_reversion_long_perp"}:
        status = "standard" if allow_short_spot else "conditional"
        return {
            "status": status,
            "requires_short_spot": True,
            "legs": ["long perpetual", "borrow and short spot if hedge is required"],
            "notes": [
                "Reverse cash-and-carry requires confirmed spot borrow or a margin venue.",
                "Without spot borrow, this becomes a directional long-perp trade, not a hedged arb.",
            ],
        }
    return {
        "status": "watch_only",
        "requires_short_spot": False,
        "legs": [],
        "notes": ["Signal is not strong or clean enough for execution review."],
    }


def score_candidate(row: dict) -> float:
    funding_signal = min(abs(row["funding_bps"]) * 4.0, 30.0)
    basis_signal = min(abs(row["basis_bps"]) * 0.7, 35.0)
    momentum_context = min(abs(row["change_24h_pct"]) * 0.6, 12.0)
    liquidity = row["liquidity_score"] * 20.0
    spread_penalty = min(row["spread_bps"] * 2.5, 25.0)
    mixed_penalty = 8.0 if row["direction"] == "watch_only" else 0.0
    feasibility_penalty = 12.0 if row.get("execution_feasibility", {}).get("status") == "conditional" else 0.0
    score = (
        funding_signal
        + basis_signal
        + momentum_context
        + liquidity
        - spread_penalty
        - mixed_penalty
        - feasibility_penalty
    )
    return round(max(0.0, score), 2)


def get_funding(inst_id: str) -> dict:
    data = fetch_json("/api/v5/public/funding-rate", {"instId": inst_id})
    return data["data"][0] if data.get("data") else {}


def get_funding_history(inst_id: str, limit: int = 20) -> dict:
    data = fetch_json("/api/v5/public/funding-rate-history", {"instId": inst_id, "limit": str(limit)})
    rows = data.get("data") or []
    values = [as_float(row.get("fundingRate")) * 10_000.0 for row in rows if row.get("fundingRate") not in (None, "")]
    if not values:
        return {"funding_history_count": 0}
    newest = values[0]
    oldest = values[-1]
    return {
        "funding_history_count": len(values),
        "funding_history_avg_bps": round(sum(values) / len(values), 4),
        "funding_history_min_bps": round(min(values), 4),
        "funding_history_max_bps": round(max(values), 4),
        "funding_history_last_bps": round(newest, 4),
        "funding_history_slope_bps": round(newest - oldest, 4),
    }


def _safe_public_map(path: str, params: dict[str, str], key: str, value_keys: tuple[str, ...]) -> dict[str, dict]:
    try:
        rows = fetch_json(path, params).get("data") or []
    except Exception:
        return {}
    output: dict[str, dict] = {}
    for row in rows:
        inst_id = str(row.get(key) or "")
        if not inst_id:
            continue
        output[inst_id] = {name: row.get(name) for name in value_keys}
    return output


def _funding_interval_hours(funding: dict) -> float | None:
    current = funding.get("fundingTime")
    nxt = funding.get("nextFundingTime")
    try:
        if not current or not nxt:
            return None
        return round((int(nxt) - int(current)) / 3_600_000.0, 3)
    except (TypeError, ValueError):
        return None


def _basis_bucket(value: float) -> str:
    abs_value = abs(value)
    if abs_value < 10:
        return "small"
    if abs_value < 30:
        return "moderate"
    if abs_value < 75:
        return "large"
    return "extreme"


def build_scan_batch(
    scan_universe: int,
    allow_short_spot: bool = False,
    required_inst_ids: set[str] | None = None,
    enrichment_limit: int = 30,
) -> ScanBatch:
    tickers = fetch_json("/api/v5/market/tickers", {"instType": "SWAP"})["data"]
    index_rows = fetch_json("/api/v5/market/index-tickers", {"quoteCcy": "USDT"})["data"]
    index_by_id = {row["instId"]: as_float(row.get("idxPx")) for row in index_rows}
    mark_by_inst = _safe_public_map(
        "/api/v5/public/mark-price",
        {"instType": "SWAP"},
        "instId",
        ("markPx", "ts"),
    )
    open_interest_by_inst = _safe_public_map(
        "/api/v5/public/open-interest",
        {"instType": "SWAP"},
        "instId",
        ("oi", "oiCcy", "oiUsd", "ts"),
    )
    instrument_by_inst = _safe_public_map(
        "/api/v5/public/instruments",
        {"instType": "SWAP"},
        "instId",
        ("ctVal", "ctValCcy", "settleCcy", "state", "tickSz", "lotSz", "minSz"),
    )

    usdt_swaps = []
    for row in tickers:
        inst_id = row.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        quote_volume = as_float(row.get("volCcy24h"))
        if quote_volume <= 0:
            continue
        usdt_swaps.append((quote_volume, row))

    usdt_swaps.sort(key=lambda item: item[0], reverse=True)
    required_inst_ids = required_inst_ids or set()
    selected_by_id = {
        row["instId"]: row
        for _, row in usdt_swaps[:scan_universe]
    }
    for _, row in usdt_swaps:
        if row.get("instId") in required_inst_ids:
            selected_by_id[row["instId"]] = row
    selected = list(selected_by_id.values())

    funding_by_inst: dict[str, dict] = {}
    history_by_inst: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(get_funding, row["instId"]): row["instId"] for row in selected}
        for future in concurrent.futures.as_completed(futures):
            inst_id = futures[future]
            try:
                funding_by_inst[inst_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                funding_by_inst[inst_id] = {"error": str(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        history_targets = selected[: max(0, int(enrichment_limit))]
        futures = {pool.submit(get_funding_history, row["instId"]): row["instId"] for row in history_targets}
        for future in concurrent.futures.as_completed(futures):
            inst_id = futures[future]
            try:
                history_by_inst[inst_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                history_by_inst[inst_id] = {"funding_history_error": str(exc)[:180]}

    candidates = []
    seen_at = dt.datetime.now(dt.timezone.utc).isoformat()
    direction_counts: collections.Counter[str] = collections.Counter()
    for row in selected:
        inst_id = row["instId"]
        index_id = inst_id.replace("-SWAP", "")
        idx_px = index_by_id.get(index_id, 0.0)
        last = as_float(row.get("last"))
        mark_row = mark_by_inst.get(inst_id, {})
        mark_px = as_float(mark_row.get("markPx"), last)
        bid = as_float(row.get("bidPx"))
        ask = as_float(row.get("askPx"))
        open_24h = as_float(row.get("open24h"))
        quote_volume = as_float(row.get("volCcy24h"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_bps = ((ask - bid) / mid * 10_000.0) if ask > bid and mid > 0 else 999.0
        basis_bps = bps(last, idx_px) if idx_px > 0 else 0.0
        mark_basis_bps = bps(mark_px, idx_px) if idx_px > 0 else basis_bps
        change_24h_pct = bps(last, open_24h) / 100.0 if open_24h > 0 else 0.0

        funding = funding_by_inst.get(inst_id, {})
        funding_rate = as_float(funding.get("fundingRate"))
        funding_bps = funding_rate * 10_000.0
        next_funding_time = unix_ms_to_iso(funding.get("nextFundingTime") or funding.get("fundingTime"))
        funding_interval_hours = _funding_interval_hours(funding)
        direction, thesis = classify_direction(funding_bps, basis_bps)
        feasibility = execution_feasibility(direction, allow_short_spot)
        oi = open_interest_by_inst.get(inst_id, {})
        inst = instrument_by_inst.get(inst_id, {})
        history = history_by_inst.get(inst_id, {"funding_history_count": 0})

        candidate = {
            "seen_at": seen_at,
            "venue": "OKX",
            "inst_id": inst_id,
            "trade_type": "perp_funding_basis",
            "direction": direction,
            "execution_feasibility": feasibility,
            "thesis": thesis,
            "last": last,
            "index_px": idx_px,
            "mark_px": mark_px,
            "basis_bps": round(basis_bps, 3),
            "mark_basis_bps": round(mark_basis_bps, 3),
            "basis_bucket": _basis_bucket(basis_bps),
            "funding_rate": funding_rate,
            "funding_bps": round(funding_bps, 3),
            "funding_interval_hours": funding_interval_hours,
            "next_funding_time": next_funding_time,
            "change_24h_pct": round(change_24h_pct, 3),
            "quote_volume_24h": quote_volume,
            "open_interest_contracts": as_float(oi.get("oi"), None),
            "open_interest_ccy": as_float(oi.get("oiCcy"), None),
            "open_interest_usd": as_float(oi.get("oiUsd"), None),
            "contract_value": as_float(inst.get("ctVal"), None),
            "contract_value_ccy": inst.get("ctValCcy"),
            "settle_ccy": inst.get("settleCcy"),
            "instrument_state": inst.get("state"),
            **history,
            "liquidity_score": round(liquidity_score(quote_volume), 3),
            "spread_bps": round(spread_bps, 3),
            "risk_notes": [
                "paper-trade only",
                "funding can change before settlement",
                "basis can widen during momentum or liquidation cascades",
                "fees, borrow, spot leg availability, and venue risk are not fully modeled",
            ],
        }
        candidate["score"] = score_candidate(candidate)
        candidates.append(candidate)
        direction_counts[direction] += 1

    candidates.sort(key=lambda item: item["score"], reverse=True)
    observations = [
        {
            "inst_id": row.get("instId"),
            "venue": "OKX",
            "trade_type": "perp_funding_basis",
            "last": as_float(row.get("last")),
            "observed_at": seen_at,
            "price_source": "OKX public REST market tickers",
        }
        for _, row in usdt_swaps
        if as_float(row.get("last")) > 0
    ]
    return ScanBatch(
        source="OKX public REST",
        candidates=candidates,
        observations=observations,
        generated_at=seen_at,
        metadata={
            "priced_instrument_count": len(observations),
            "direction_counts": dict(direction_counts),
            "enriched_history_count": len(history_by_inst),
            "open_interest_count": len(open_interest_by_inst),
            "mark_price_count": len(mark_by_inst),
            "required_inst_id_count": len(required_inst_ids),
            "selected_instrument_count": len(selected),
        },
    )


def build_candidates(scan_universe: int, allow_short_spot: bool = False) -> list[dict]:
    return build_scan_batch(scan_universe, allow_short_spot=allow_short_spot).candidates


def write_outputs(candidates: list[dict]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "OKX public REST API",
        "mode": "research_paper_only",
        "candidates": candidates,
    }
    stamped_path = RUNS_DIR / f"opportunities_{stamp}.json"
    latest_path = RUNS_DIR / "latest_opportunities.json"
    stamped_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return stamped_path


def print_table(candidates: list[dict], limit: int) -> None:
    print("Rank | Score | Instrument | Direction | Fund bps | Basis bps | Spread bps | 24h % | Vol 24h")
    print("-" * 112)
    for idx, row in enumerate(candidates[:limit], start=1):
        print(
            f"{idx:>4} | {row['score']:>5.1f} | "
            f"{row['inst_id']:<18} | {row['direction']:<31} | "
            f"{row['funding_bps']:>8.3f} | {row['basis_bps']:>9.3f} | "
            f"{row['spread_bps']:>10.3f} | {row['change_24h_pct']:>6.2f} | "
            f"{row['quote_volume_24h']:>10.0f}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan OKX perp funding/basis dislocations.")
    parser.add_argument("--top", type=int, default=20, help="rows to print")
    parser.add_argument("--scan-universe", type=int, default=80, help="top-volume USDT swaps to scan")
    parser.add_argument(
        "--allow-short-spot",
        action="store_true",
        help="treat reverse cash-and-carry ideas as executable after external borrow confirmation",
    )
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        candidates = build_candidates(args.scan_universe, allow_short_spot=args.allow_short_spot)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1

    path = write_outputs(candidates)
    elapsed = time.perf_counter() - started
    print_table(candidates, args.top)
    print()
    print(f"Wrote {path}")
    print(f"Scanned {len(candidates)} instruments in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
