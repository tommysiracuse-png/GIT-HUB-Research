"""Public regional FX reference feed for paper-only frontier normalization."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import urllib.parse
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
REPORT_JSON = RUNS_DIR / "regional_fx_reference_latest.json"

DEFAULT_QUOTES = (
    "ARS",
    "BRL",
    "CLP",
    "COP",
    "GHS",
    "IDR",
    "KES",
    "MXN",
    "MYR",
    "NGN",
    "PEN",
    "PHP",
    "SGD",
    "THB",
    "ZAR",
)
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rates"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _timestamp_to_iso(value: object) -> str | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _age_seconds(value: str | None) -> float | None:
    parsed = _parse_iso(value)
    if not parsed:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def fetch_json(url: str, timeout: int = 10) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 inefficiency-radar/0.2",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_exchange_rate_api(payload: dict, fetched_at: str, quotes: set[str] | None = None) -> list[dict]:
    wanted = quotes or set(DEFAULT_QUOTES)
    rates = payload.get("rates") or {}
    provider_updated = payload.get("time_last_update_utc") or _timestamp_to_iso(payload.get("time_last_update_unix"))
    next_update = payload.get("time_next_update_utc") or _timestamp_to_iso(payload.get("time_next_update_unix"))
    rows = []
    for quote in sorted(wanted):
        rate = rates.get(quote)
        try:
            rate_value = float(rate)
        except (TypeError, ValueError):
            continue
        if rate_value <= 0:
            continue
        rows.append(
            {
                "provider": "ExchangeRate-API Open",
                "base": "USD",
                "quote": quote,
                "rate": rate_value,
                "fetched_at": fetched_at,
                "provider_updated_at": provider_updated,
                "next_update_at": next_update,
                "status": "ok",
                "source_url": EXCHANGE_RATE_API_URL,
            }
        )
    return rows


def parse_frankfurter(payload: dict, fetched_at: str, quotes: set[str] | None = None) -> list[dict]:
    wanted = quotes or set(DEFAULT_QUOTES)
    rates = payload.get("rates") or {}
    provider_updated = payload.get("date")
    if provider_updated and len(str(provider_updated)) == 10:
        provider_updated = f"{provider_updated}T00:00:00+00:00"
    rows = []
    for quote in sorted(wanted):
        rate = rates.get(quote)
        try:
            rate_value = float(rate)
        except (TypeError, ValueError):
            continue
        if rate_value <= 0:
            continue
        rows.append(
            {
                "provider": "Frankfurter",
                "base": "USD",
                "quote": quote,
                "rate": rate_value,
                "fetched_at": fetched_at,
                "provider_updated_at": provider_updated,
                "next_update_at": None,
                "status": "ok",
                "source_url": FRANKFURTER_URL,
            }
        )
    return rows


def _configured_quotes(settings: dict | None, quotes: set[str] | None = None) -> set[str]:
    if quotes:
        return {str(item).upper() for item in quotes}
    cfg = (settings or {}).get("frontier_regional_fx", {})
    configured = cfg.get("quotes") or DEFAULT_QUOTES
    return {str(item).upper() for item in configured}


def _latest_cached(
    conn: sqlite3.Connection,
    quotes: set[str],
    *,
    max_age_hours: float,
) -> dict[str, dict]:
    rows = conn.execute(
        """
        select provider, base, quote, rate, fetched_at, provider_updated_at,
               next_update_at, status, source_url
        from regional_fx_snapshots
        where base = 'USD' and status = 'ok' and quote in ({})
        order by fetched_at desc, id desc
        """.format(",".join("?" for _ in quotes)),
        tuple(sorted(quotes)),
    ).fetchall()
    output: dict[str, dict] = {}
    max_age_seconds = max_age_hours * 3600.0
    for row in rows:
        quote = str(row["quote"])
        if quote in output:
            continue
        age = _age_seconds(row["fetched_at"])
        stale = age is None or age > max_age_seconds
        output[quote] = {**dict(row), "age_seconds": round(age, 3) if age is not None else None, "stale": stale}
    return output


def _insert_snapshots(conn: sqlite3.Connection, rows: list[dict], payload: dict) -> None:
    payload_json = json.dumps(payload, sort_keys=True)[:50000]
    for row in rows:
        conn.execute(
            """
            insert or ignore into regional_fx_snapshots (
                fetched_at, provider, base, quote, rate, provider_updated_at,
                next_update_at, status, source_url, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["fetched_at"],
                row["provider"],
                row["base"],
                row["quote"],
                row["rate"],
                row.get("provider_updated_at"),
                row.get("next_update_at"),
                row["status"],
                row["source_url"],
                payload_json,
            ),
        )
    conn.commit()


def _record_provider_error(conn: sqlite3.Connection, provider: str, source_url: str, fetched_at: str, exc: Exception) -> None:
    conn.execute(
        """
        insert into regional_fx_snapshots (
            fetched_at, provider, base, quote, rate, provider_updated_at,
            next_update_at, status, source_url, payload_json
        ) values (?, ?, 'USD', 'ERROR', null, null, null, ?, ?, ?)
        """,
        (fetched_at, provider, f"error:{str(exc)[:160]}", source_url, "{}"),
    )
    conn.commit()


def _write_report(summary: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")


def get_regional_fx_references(
    conn: sqlite3.Connection | None,
    settings: dict | None = None,
    *,
    quotes: set[str] | None = None,
    force_refresh: bool = False,
) -> dict[str, dict]:
    cfg = (settings or {}).get("frontier_regional_fx", {})
    if not cfg.get("enabled", True) or conn is None:
        return {}
    wanted = _configured_quotes(settings, quotes)
    if not wanted:
        return {}
    cache_ttl_minutes = float(cfg.get("cache_ttl_minutes", 60))
    max_age_hours = float(cfg.get("max_reference_age_hours", 30))
    cached = _latest_cached(conn, wanted, max_age_hours=max_age_hours)
    fresh_enough = cached and all(
        quote in cached
        and cached[quote].get("age_seconds") is not None
        and float(cached[quote]["age_seconds"]) <= cache_ttl_minutes * 60.0
        for quote in wanted
    )
    provider_status = []
    if not force_refresh and fresh_enough:
        summary = _summary(cached, provider_status, from_cache=True)
        _write_report(summary)
        return {quote: row for quote, row in cached.items() if not row.get("stale")}

    fetched_at = _utc_now()
    provider_attempts = [
        ("ExchangeRate-API Open", EXCHANGE_RATE_API_URL, parse_exchange_rate_api),
        (
            "Frankfurter",
            FRANKFURTER_URL + "?" + urllib.parse.urlencode({"base": "USD", "quotes": ",".join(sorted(wanted))}),
            parse_frankfurter,
        ),
    ]
    for provider, url, parser in provider_attempts:
        try:
            payload = fetch_json(url, timeout=int(cfg.get("timeout_seconds", 10)))
            rows = parser(payload, fetched_at, wanted)
            if rows:
                _insert_snapshots(conn, rows, payload)
                provider_status.append({"provider": provider, "status": "ok", "row_count": len(rows)})
                cached = _latest_cached(conn, wanted, max_age_hours=max_age_hours)
                summary = _summary(cached, provider_status, from_cache=False)
                _write_report(summary)
                return {quote: row for quote, row in cached.items() if not row.get("stale")}
            provider_status.append({"provider": provider, "status": "empty", "row_count": 0})
        except Exception as exc:  # noqa: BLE001
            provider_status.append({"provider": provider, "status": "error", "error": str(exc)[:160]})
            _record_provider_error(conn, provider, url, fetched_at, exc)

    cached = _latest_cached(conn, wanted, max_age_hours=max_age_hours)
    summary = _summary(cached, provider_status, from_cache=True)
    _write_report(summary)
    return {quote: row for quote, row in cached.items() if not row.get("stale")}


def _summary(references: dict[str, dict], provider_status: list[dict], *, from_cache: bool) -> dict:
    return {
        "generated_at": _utc_now(),
        "paper_only": True,
        "from_cache": from_cache,
        "reference_count": len([row for row in references.values() if not row.get("stale")]),
        "stale_count": len([row for row in references.values() if row.get("stale")]),
        "provider_status": provider_status,
        "references": references,
        "hard_limits": [
            "Public no-key FX reference data only.",
            "Used only for paper/shadow frontier normalization.",
            "Does not enable live trading, credentials, account APIs, or broker routes.",
        ],
    }
