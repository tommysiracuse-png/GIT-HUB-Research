#!/usr/bin/env python3
"""Crypto public venue health scanner.

Maps which venues are reachable from this machine. This is route discovery, not
trading.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"


VENUES = [
    {
        "venue": "coinbase",
        "url": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        "route_id": "coinbase_spot_public",
        "asset": "BTC-USD spot",
    },
    {
        "venue": "okx",
        "url": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP",
        "route_id": "okx_perp_public",
        "asset": "BTC-USDT swap",
    },
    {
        "venue": "kraken",
        "url": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
        "route_id": "kraken_spot_public",
        "asset": "XBTUSD spot",
    },
    {
        "venue": "bybit",
        "url": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
        "route_id": "bybit_perp_public",
        "asset": "BTCUSDT linear",
    },
    {
        "venue": "binance_us",
        "url": "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT",
        "route_id": "binance_us_spot_public",
        "asset": "BTCUSDT spot",
    },
]


def fetch(url: str, timeout: int = 8) -> tuple[bool, str, float]:
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 inefficiency-radar/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response.read(2048)
            return True, str(response.status), (time.perf_counter() - start) * 1000.0
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300], (time.perf_counter() - start) * 1000.0


def scan() -> list[dict]:
    rows = []
    for venue in VENUES:
        ok, status, latency = fetch(venue["url"])
        rows.append(
            {
                **venue,
                "reachable": ok,
                "status": status,
                "latency_ms": round(latency, 2),
                "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
    return rows


def write_outputs(rows: list[dict]) -> pathlib.Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / "crypto_venue_health.json"
    path.write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "venues": rows}, indent=2), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan public crypto venue reachability.")
    parser.parse_args(argv)
    rows = scan()
    path = write_outputs(rows)
    for row in rows:
        print(f"{row['venue']:<12} reachable={str(row['reachable']):<5} latency={row['latency_ms']:>8.1f}ms status={row['status'][:80]}")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
