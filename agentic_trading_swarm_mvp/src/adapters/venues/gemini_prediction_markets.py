"""Gemini public BTC 5-minute prediction-market observations."""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, parse_json, utc_now
from scan_batch import ScanBatch


API_ROOT = "https://api.gemini.com"
EVENTS_ENDPOINT = API_ROOT + "/v1/prediction-markets/events"
BTCUSD_TICKER_URL = API_ROOT + "/v1/pubticker/BTCUSD"
DOCS_URL = "https://developer.gemini.com/prediction-markets/prediction-markets"
EVENTS_DOCS_URL = "https://developer.gemini.com/rest-api/prediction-markets/events"
TICKERS_DOCS_URL = "https://developer.gemini.com/prediction-markets/tickers-crypto"
MARKET_SURFACE = "gemini_btc_5m_prediction_markets"
INSTRUMENT_RE = re.compile(
    r"^GEMI-(?P<underlying>[A-Z]+)(?:(?P<duration>05M|15M))?(?P<expiry>\d{10})-(?P<contract>UP|HI(?P<encoded_strike>\d+(?:D\d+)?))$"
)


class GeminiPredictionMarketsParseError(ValueError):
    """Raised when Gemini's public prediction-market schema drifts."""


def events_url(limit: int = 50) -> str:
    query = urllib.parse.urlencode(
        {
            "status": "active",
            "category": "crypto",
            "limit": max(1, min(int(limit), 1000)),
        }
    )
    return f"{EVENTS_ENDPOINT}?{query}"


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _probability(value: Any) -> float | None:
    parsed = _float(value)
    return parsed if parsed is not None and 0.0 <= parsed <= 1.0 else None


def _parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    if re.fullmatch(r"\d{10}", text):
        try:
            return dt.datetime.strptime(text, "%y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def _encoded_strike(value: str | None) -> float | None:
    if not value:
        return None
    return _float(value.replace("D", "."))


def parse_gemini_btcusd_ticker(
    payload: object,
    *,
    received_at: str | None = None,
    source_url: str = BTCUSD_TICKER_URL,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GeminiPredictionMarketsParseError("Gemini BTCUSD ticker response must be an object")
    bid = _float(payload.get("bid"))
    ask = _float(payload.get("ask"))
    last = _float(payload.get("last"))
    if bid is None and ask is None and last is None:
        raise GeminiPredictionMarketsParseError("Gemini BTCUSD ticker response must contain bid, ask, or last")
    observed_at = received_at or utc_now()
    return {
        "symbol": "BTCUSD",
        "bid": bid,
        "ask": ask,
        "last": last,
        "mid": ((bid + ask) / 2.0) if bid is not None and ask is not None and bid <= ask else last,
        "observed_at": observed_at,
        "source_url": source_url,
    }


def parse_gemini_btc_5m_events(
    payload: object,
    *,
    received_at: str | None = None,
    source_url: str | None = None,
    btc_spot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize active BTC 5-minute contracts from Gemini's public event feed."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise GeminiPredictionMarketsParseError("Gemini prediction-markets response must contain a data array")
    observed_at = received_at or utc_now()
    url = source_url or events_url()
    spot_mid = _float((btc_spot or {}).get("mid"))
    spot_bid = _float((btc_spot or {}).get("bid"))
    spot_ask = _float((btc_spot or {}).get("ask"))
    output: list[dict[str, Any]] = []
    invalid_rows = 0
    for event in payload["data"]:
        if not isinstance(event, dict):
            invalid_rows += 1
            continue
        event_ticker = str(event.get("ticker") or "").strip().upper()
        series = str(event.get("series") or "").strip().upper()
        contracts = event.get("contracts")
        if not event_ticker or not isinstance(contracts, list):
            invalid_rows += 1
            continue
        for contract in contracts:
            if not isinstance(contract, dict):
                invalid_rows += 1
                continue
            symbol = str(contract.get("instrumentSymbol") or "").strip().upper()
            match = INSTRUMENT_RE.fullmatch(symbol)
            if not match:
                invalid_rows += 1
                continue
            if match.group("underlying") != "BTC" or match.group("duration") != "05M":
                continue
            if str(contract.get("status") or "").strip().lower() != "active":
                continue
            if str(contract.get("marketState") or "").strip().lower() != "open":
                continue
            prices = contract.get("prices")
            if not isinstance(prices, dict):
                invalid_rows += 1
                continue
            buy = prices.get("buy") if isinstance(prices.get("buy"), dict) else {}
            sell = prices.get("sell") if isinstance(prices.get("sell"), dict) else {}
            yes_bid = _probability(prices.get("bestBid"))
            yes_ask = _probability(prices.get("bestAsk"))
            no_bid = None
            no_ask = None
            if yes_bid is None:
                yes_bid = _probability(sell.get("yes"))
            if yes_ask is None:
                yes_ask = _probability(buy.get("yes"))
            no_bid = _probability(sell.get("no"))
            no_ask = _probability(buy.get("no"))
            if yes_ask is None and no_bid is not None:
                yes_ask = round(1.0 - no_bid, 8)
            if yes_bid is None and no_ask is not None:
                yes_bid = round(1.0 - no_ask, 8)
            if no_ask is None and yes_bid is not None:
                no_ask = round(1.0 - yes_bid, 8)
            if no_bid is None and yes_ask is not None:
                no_bid = round(1.0 - yes_ask, 8)
            last_trade = _probability(prices.get("lastTradePrice"))
            if yes_bid is not None and yes_ask is not None and yes_bid <= yes_ask:
                last = (yes_bid + yes_ask) / 2.0
                price_type = "yes_bid_ask_midpoint"
            elif last_trade is not None:
                last = last_trade
                price_type = "last_trade_probability"
            elif yes_bid is not None:
                last = yes_bid
                price_type = "yes_bid_probability"
            elif yes_ask is not None:
                last = yes_ask
                price_type = "yes_ask_probability"
            else:
                invalid_rows += 1
                continue
            strike = contract.get("strike") if isinstance(contract.get("strike"), dict) else {}
            strike_price = _float(strike.get("value"))
            if strike_price is None:
                strike_price = _encoded_strike(match.group("encoded_strike"))
            expiry = _parse_time(contract.get("expiryDate") or event.get("expiryDate") or match.group("expiry"))
            effective = _parse_time(
                contract.get("effectiveDate") or event.get("startTime") or event.get("effectiveDate")
            )
            observed_dt = _parse_time(observed_at) or dt.datetime.now(dt.timezone.utc)
            if expiry and observed_dt >= expiry:
                session_status = "expired"
            elif str(contract.get("marketState") or "").strip().lower() == "open":
                session_status = "open"
            else:
                session_status = str(contract.get("marketState") or event.get("status") or "unknown").strip().lower() or "unknown"
            spread = yes_ask - yes_bid if yes_bid is not None and yes_ask is not None and yes_bid <= yes_ask else None
            spot_gap = None if spot_mid is None or strike_price is None else spot_mid - strike_price
            implied_direction = None
            if spot_mid is not None and strike_price is not None:
                implied_direction = "buy_yes_event" if spot_mid >= strike_price else "buy_no_event"
            elif last >= 0.5:
                implied_direction = "buy_yes_event"
            else:
                implied_direction = "buy_no_event"
            output.append(
                {
                    "venue": "GEMINI",
                    "inst_id": symbol,
                    "instrument_id": symbol,
                    "symbol": symbol,
                    "event_ticker": event_ticker,
                    "contract_ticker": str(contract.get("ticker") or match.group("contract") or "").upper() or None,
                    "title": str(event.get("title") or symbol).strip() or symbol,
                    "base": "BTC",
                    "quote": "USD",
                    "market_type": "prediction_market",
                    "market_surface": MARKET_SURFACE,
                    "asset_class": "prediction_markets",
                    "instrument_family": "binary_event_contract",
                    "trade_type": "prediction_market_probability",
                    "signal_surface": "prediction_market_probability",
                    "direction": implied_direction,
                    "last": round(last, 8),
                    "yes_probability": round(last, 8),
                    "no_probability": round(1.0 - last, 8),
                    "price_type": price_type,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "spread": round(spread, 8) if spread is not None else None,
                    "spread_bps_of_payout": round(spread * 10_000.0, 3) if spread is not None else None,
                    "last_trade_probability": last_trade,
                    "event_volume_usd": _float(event.get("volume")),
                    "event_volume_24h_usd": _float(event.get("volume24h")),
                    "series": series or None,
                    "category": str(event.get("category") or "").strip() or None,
                    "template": str(event.get("template") or "").strip() or None,
                    "event_status": str(event.get("status") or "").strip() or None,
                    "market_state": str(contract.get("marketState") or "").strip() or None,
                    "event_is_live": bool(event.get("isLive")),
                    "strike_price": strike_price,
                    "strike_type": str(strike.get("type") or "").strip() or None,
                    "strike_available_at": str(strike.get("availableAt") or "").strip() or None,
                    "effective_date": effective.isoformat() if effective else None,
                    "expiry": expiry.isoformat() if expiry else None,
                    "time_to_expiry_seconds": round((expiry - observed_dt).total_seconds(), 3) if expiry else None,
                    "contract_window_seconds": round((expiry - effective).total_seconds(), 3)
                    if expiry and effective
                    else None,
                    "underlying_spot_price": spot_mid,
                    "underlying_bid": spot_bid,
                    "underlying_ask": spot_ask,
                    "spot_strike_gap_usd": round(spot_gap, 8) if spot_gap is not None else None,
                    "spot_strike_gap_bps": round((spot_gap / strike_price) * 10_000.0, 3)
                    if spot_gap is not None and strike_price
                    else None,
                    "reference_source": str(contract.get("source") or event.get("source") or "").strip() or None,
                    "source_agency": (
                        str(((event.get("sourceDetails") or {}).get("agency")) or "").strip() or None
                    ),
                    "source_index": (
                        str(((event.get("sourceDetails") or {}).get("index")) or "").strip() or None
                    ),
                    "paper_experiment_eligible": bool(
                        session_status == "open" and last is not None and strike_price is not None
                    ),
                    "paper_only": True,
                    "read_only": True,
                    "execution_disabled": True,
                    "order_routing_disabled": True,
                    "data_status": "reachable",
                    "fetch_status": "reachable",
                    "quality_status": "official_prediction_market_quote",
                    "freshness_state": "fresh",
                    "freshness_basis": "response_received",
                    "freshness_age_seconds": 0.0,
                    "session_status": session_status,
                    "observed_at": observed_at,
                    "fetched_at": observed_at,
                    "price_source": "Gemini public prediction-markets events",
                    "source_url": url,
                    "spot_source_url": (btc_spot or {}).get("source_url"),
                    "documentation_url": DOCS_URL,
                }
            )
    if payload["data"] and not output and invalid_rows:
        raise GeminiPredictionMarketsParseError(
            f"Gemini prediction-markets response contained no usable BTC 5-minute contracts; {invalid_rows} invalid rows"
        )
    output.sort(
        key=lambda row: (
            float(row.get("event_volume_24h_usd") or 0.0),
            -abs(float(row.get("spot_strike_gap_bps") or 0.0)),
        ),
        reverse=True,
    )
    return output


def _fetch_evidence(url: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": url,
        "fetch_status": str(result.get("status") or "unavailable"),
        "http_status": result.get("http_status"),
        "fetched_at": result.get("received_at"),
        "latency_ms": result.get("latency_ms"),
    }


def _failure_observation(
    result: dict[str, Any],
    source_url: str,
    *,
    parser_error: str | None = None,
    empty_window: bool = False,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("GEMINI", source_url, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "response_received" if empty_window else "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "documentation_url": DOCS_URL,
            "paper_only": True,
            "read_only": True,
            "execution_disabled": True,
            "order_routing_disabled": True,
            "candidate_reject_reason": (
                "public_prediction_market_parser_failure"
                if parser_error
                else "public_prediction_market_active_contract_window_empty"
                if empty_window
                else "public_prediction_market_source_unavailable"
            ),
        }
    )
    if empty_window:
        observation["data_status"] = "reachable"
        observation["notes"] = ["Gemini prediction-markets feed was reachable but no active BTC 5-minute contract was available."]
    return observation


class GeminiBtcFiveMinutePredictionMarketsAdapter:
    info = AdapterInfo(
        adapter_id="gemini_prediction_markets_btc_5m",
        venue="GEMINI",
        market_type="prediction_market",
        source="Gemini official public prediction-markets REST API",
        capabilities=(
            "market_catalog",
            "ticker",
            "best_bid_ask",
            "last_trade",
            "strike_price",
            "event_schedule",
            "source_health",
        ),
        aliases=(
            "gemini",
            "gemini prediction markets",
            "gemini btc 5 minute prediction markets",
            "gemi btc05m",
        ),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.gemini_prediction_markets.GeminiBtcFiveMinutePredictionMarketsAdapter",
        quote_assets=("USD",),
        default_cache_minutes=0,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        timeout = max(1, int(cfg.get("timeout_seconds", 12)))
        catalog_url = events_url(max(1, min(int(cfg.get("event_limit", 50)), 1000)))
        events_result = fetch_text(catalog_url, timeout)
        fetch_status: dict[str, dict[str, Any]] = {"events": _fetch_evidence(catalog_url, events_result)}
        parser_failures: list[dict[str, str]] = []

        btc_spot = None
        spot_result = fetch_text(BTCUSD_TICKER_URL, timeout)
        fetch_status["btc_spot"] = _fetch_evidence(BTCUSD_TICKER_URL, spot_result)
        if spot_result.get("ok"):
            try:
                btc_spot = parse_gemini_btcusd_ticker(
                    parse_json(spot_result.get("text") or ""),
                    received_at=spot_result.get("received_at"),
                )
            except (GeminiPredictionMarketsParseError, TypeError, ValueError) as exc:
                parser_failures.append(
                    {
                        "source_url": BTCUSD_TICKER_URL,
                        "error": f"Gemini BTCUSD ticker parser failed: {exc}"[:300],
                    }
                )

        if not events_result.get("ok"):
            observation = _failure_observation(events_result, catalog_url)
            return self._batch([observation], str(events_result.get("status") or "unavailable"), fetch_status, parser_failures)

        try:
            observations = parse_gemini_btc_5m_events(
                parse_json(events_result.get("text") or ""),
                received_at=events_result.get("received_at"),
                source_url=catalog_url,
                btc_spot=btc_spot,
            )
        except (GeminiPredictionMarketsParseError, TypeError, ValueError) as exc:
            message = f"Gemini prediction-markets parser failed: {exc}"[:300]
            parser_failures.append({"source_url": catalog_url, "error": message})
            observation = _failure_observation(events_result, catalog_url, parser_error=message)
            return self._batch([observation], "degraded", fetch_status, parser_failures)

        if not observations:
            observation = _failure_observation(events_result, catalog_url, empty_window=True)
            return self._batch([observation], "reachable", fetch_status, parser_failures)

        source_status = "degraded" if parser_failures else "reachable"
        return self._batch(observations, source_status, fetch_status, parser_failures)

    def _batch(
        self,
        observations: list[dict[str, Any]],
        source_status: str,
        fetch_status: dict[str, dict[str, Any]],
        parser_failures: list[dict[str, str]],
    ) -> ScanBatch:
        real_count = sum(
            1
            for row in observations
            if row.get("data_status") == "reachable"
            and row.get("last") is not None
            and row.get("direction") != "watch_only"
            and not row.get("candidate_reject_reason")
        )
        freshness_states = sorted({str(row.get("freshness_state") or "unknown") for row in observations})
        session_state = sorted({str(row.get("session_status") or "unknown") for row in observations})
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 1170,
                "source_status": source_status,
                "source_urls": [entry["source_url"] for entry in fetch_status.values()],
                "documentation_urls": [DOCS_URL, EVENTS_DOCS_URL, TICKERS_DOCS_URL],
                "fetch_status": fetch_status,
                "freshness_state": "fresh" if "fresh" in freshness_states else freshness_states[0],
                "freshness_states": freshness_states,
                "session_state": session_state,
                "parser_failures": parser_failures,
                "observation_count": len(observations),
                "real_observation_count": real_count,
                "candidate_count": 0,
                "paper_only": True,
            },
        )


register_adapter(GeminiBtcFiveMinutePredictionMarketsAdapter())
