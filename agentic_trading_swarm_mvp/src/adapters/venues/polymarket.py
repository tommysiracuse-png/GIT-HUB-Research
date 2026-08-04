"""Anonymous Polymarket live-sports state observations for paper research."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Iterable

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import health_observation, slug, utc_now
from scan_batch import ScanBatch


SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
DOCS_URL = "https://docs.polymarket.com/market-data/websocket/sports"
API_REFERENCE_URL = "https://docs.polymarket.com/api-reference/wss/sports"
MARKET_SURFACE = "polymarket_live_sports_state"


class PolymarketSportsParseError(ValueError):
    """Raised when a sports-stream frame does not match the public schema."""


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise PolymarketSportsParseError(f"Polymarket sports {field} must be a boolean")


def _parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _sport_family(league: str) -> str:
    league = league.upper()
    if league in {"NFL", "CFB", "NCAAF"}:
        return "american_football"
    if league == "NHL":
        return "ice_hockey"
    if league == "MLB":
        return "baseball"
    if league in {"NBA", "CBB", "NCAAB", "WNBA"}:
        return "basketball"
    if league.startswith(("ATP", "WTA", "ITF")) or league == "TENNIS":
        return "tennis"
    if league in {"LOL", "DOTA2", "CS2", "CSGO", "VALORANT", "ESPORTS"}:
        return "esports"
    if league in {
        "SOCCER",
        "EPL",
        "MLS",
        "UCL",
        "UEL",
        "LALIGA",
        "BUNDESLIGA",
        "SERIEA",
        "LIGUE1",
    }:
        return "soccer"
    return "other_sport"


def _session_status(status: str, *, live: bool, ended: bool) -> str:
    normalized = status.casefold().replace("_", "").replace(" ", "")
    if ended or normalized in {
        "final",
        "f/ot",
        "f/so",
        "finished",
        "awarded",
        "forfeit",
        "notnecessary",
    }:
        return "ended"
    if normalized in {"canceled", "cancelled"}:
        return "canceled"
    if live or normalized in {"inprogress", "running", "penaltyshootout"}:
        return "live"
    if normalized in {"break", "halftime"}:
        return "intermission"
    if normalized in {"suspended", "postponed", "delayed"}:
        return "interrupted"
    if normalized in {"scheduled", "notstarted"}:
        return "scheduled"
    return "unknown"


def parse_polymarket_sports_message(
    message: object,
    *,
    received_at: str | None = None,
    source_url: str = SPORTS_WS_URL,
    stale_after_seconds: float = 30.0,
) -> dict[str, Any]:
    """Normalize a bare WebSocket game object or documented SDK envelope."""

    if isinstance(message, (str, bytes, bytearray)):
        try:
            message = json.loads(message)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolymarketSportsParseError("Polymarket sports frame must be valid JSON") from exc
    if not isinstance(message, dict):
        raise PolymarketSportsParseError("Polymarket sports frame must be an object")

    if "payload" in message:
        if message.get("topic") not in (None, "sports") or message.get("type") not in (
            None,
            "sport_result",
        ):
            raise PolymarketSportsParseError("Polymarket sports envelope has an unexpected topic or type")
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise PolymarketSportsParseError("Polymarket sports envelope payload must be an object")
    else:
        payload = message

    game_id = _first(payload, "gameId", "game_id")
    game_slug = str(payload.get("slug") or "").strip()
    if game_id in (None, "") and not game_slug:
        raise PolymarketSportsParseError("Polymarket sports frame requires gameId or slug")
    league = str(_first(payload, "leagueAbbreviation", "league_abbreviation") or "").strip().upper()
    if not league:
        raise PolymarketSportsParseError("Polymarket sports frame requires leagueAbbreviation")
    status = str(payload.get("status") or "").strip()
    if not status:
        raise PolymarketSportsParseError("Polymarket sports frame requires status")
    live = _boolean(payload.get("live"), "live")
    ended = _boolean(payload.get("ended"), "ended")
    score = str(payload.get("score") or "").strip()
    score_segments = score.split("|")
    score_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", score_segments[0])
    if not score_match:
        raise PolymarketSportsParseError(
            'Polymarket sports score must begin with the documented "<home>-<away>" format'
        )
    home_score = int(score_match.group(1))
    away_score = int(score_match.group(2))
    score_components: list[dict[str, int]] = []
    for segment in score_segments[:-1] if score_segments[-1].upper().startswith("BO") else score_segments:
        component = re.fullmatch(r"(\d+)\s*-\s*(\d+)", segment)
        if component:
            score_components.append(
                {"home": int(component.group(1)), "away": int(component.group(2))}
            )

    fetched = _parse_time(received_at) or dt.datetime.now(dt.timezone.utc)
    source_updated = _parse_time(
        _first(payload, "lastUpdate", "last_update", "updatedAt", "updated_at")
    )
    observed = source_updated or fetched
    freshness_age = max(0.0, (fetched - observed).total_seconds())
    freshness_state = "stale" if freshness_age > max(0.0, stale_after_seconds) else "fresh"
    home_team = str(_first(payload, "homeTeam", "home_team") or "").strip() or None
    away_team = str(_first(payload, "awayTeam", "away_team") or "").strip() or None
    identity = str(game_id) if game_id not in (None, "") else game_slug
    instrument = f"POLYMARKET:SPORTS:{league}:{slug(identity)}"

    return {
        "venue": "POLYMARKET",
        "inst_id": instrument,
        "instrument_id": instrument,
        "symbol": game_slug or identity,
        "game_id": game_id,
        "sportradar_game_id": _first(payload, "sportradarGameId", "sportradar_game_id"),
        "game_slug": game_slug or None,
        "league_abbreviation": league,
        "sport_family": _sport_family(league),
        "home_team": home_team,
        "away_team": away_team,
        "title": " vs ".join(item for item in (away_team, home_team) if item) or game_slug or identity,
        "base": home_team or identity,
        "quote": away_team or "SCORE",
        "market_type": "sports_event_state",
        "market_surface": MARKET_SURFACE,
        "asset_class": "sports_event_data",
        "trade_type": "official_sports_state_reference",
        "direction": "watch_only",
        "last": float(home_score),
        "measurement_type": "home_score",
        "home_score": home_score,
        "away_score": away_score,
        "score": score,
        "score_components": score_components,
        "series_format": score_segments[-1] if score_segments[-1].upper().startswith("BO") else None,
        "game_status": status,
        "live": live,
        "ended": ended,
        "period": _first(payload, "period"),
        "elapsed": _first(payload, "elapsed"),
        "turn": _first(payload, "turn"),
        "finished_at": _first(payload, "finishedAt", "finished_at"),
        "source_updated_at": source_updated.isoformat() if source_updated else None,
        "data_status": "reachable",
        "fetch_status": "reachable",
        "quality_status": "official_sports_state",
        "freshness_state": freshness_state,
        "freshness_basis": "source_update" if source_updated else "response_received",
        "freshness_age_seconds": round(freshness_age, 3),
        "session_status": _session_status(status, live=live, ended=ended),
        "observed_at": observed.isoformat(),
        "fetched_at": fetched.isoformat(),
        "price_source": "Polymarket official public sports state WebSocket",
        "source_url": source_url,
        "documentation_url": DOCS_URL,
        "candidate_reject_reason": "public_sports_state_watch_only_no_execution_route",
    }


def parse_polymarket_sports_messages(
    messages: Iterable[object],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Normalize a sequence of sports frames, preserving their arrival order."""

    return [parse_polymarket_sports_message(message, **kwargs) for message in messages]


def fetch_sports_messages(
    url: str = SPORTS_WS_URL,
    *,
    connect_timeout: float = 8.0,
    listen_seconds: float = 6.0,
    max_messages: int = 100,
) -> dict[str, Any]:
    """Collect a bounded, anonymous sample from the public sports WebSocket."""

    started = time.perf_counter()
    received_at = utc_now()
    messages: list[object] = []
    heartbeat_count = 0
    connection = None
    try:
        import websocket

        connection = websocket.create_connection(url, timeout=max(0.1, connect_timeout))
        handshake_status = getattr(connection, "status", 101)
        deadline = time.monotonic() + max(0.0, listen_seconds)
        while len(messages) < max(1, max_messages) and time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            connection.settimeout(min(1.0, remaining))
            try:
                frame = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if frame is None:
                break
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", errors="replace")
            if isinstance(frame, str) and frame.strip().casefold() == "ping":
                connection.send("pong")
                heartbeat_count += 1
                continue
            if isinstance(frame, str) and frame.strip().casefold() == "pong":
                continue
            messages.append(frame)
        received_at = utc_now()
        return {
            "ok": True,
            "status": "reachable",
            "http_status": handshake_status,
            "messages": messages,
            "received_at": received_at,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "heartbeat_count": heartbeat_count,
            "connection_state": "connected",
        }
    except Exception as exc:  # noqa: BLE001 - source health must survive network/dependency failures.
        received_at = utc_now()
        partial = bool(messages)
        return {
            "ok": partial,
            "status": "degraded" if partial else "unavailable",
            "http_status": None,
            "messages": messages,
            "received_at": received_at,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "heartbeat_count": heartbeat_count,
            "connection_state": "connection_interrupted" if partial else "connection_failed",
            "error": str(exc)[:300],
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing health collection is best effort.
                pass


def _adapter_config(settings: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    root = settings.get("public_market_adapters") or {}
    nested = (root.get("adapters") or {}).get(adapter_id) or {}
    direct = root.get(adapter_id) or {}
    return {**nested, **direct}


def _failure_observation(
    result: dict[str, Any],
    *,
    parser_error: str | None = None,
    empty_window: bool = False,
) -> dict[str, Any]:
    evidence = dict(result)
    if parser_error:
        evidence.update({"status": "degraded", "error": parser_error})
    observation = health_observation("POLYMARKET", SPORTS_WS_URL, evidence, MARKET_SURFACE)
    observation.update(
        {
            "fetch_status": str(result.get("status") or "unavailable"),
            "freshness_state": "unknown",
            "freshness_basis": "parser_failure" if parser_error else "unavailable",
            "freshness_age_seconds": None,
            "parser_failure": parser_error,
            "connection_state": result.get("connection_state"),
            "documentation_url": DOCS_URL,
            "candidate_reject_reason": (
                "public_sports_stream_parser_failure"
                if parser_error
                else "public_sports_stream_observation_window_empty"
                if empty_window
                else "public_sports_stream_unavailable"
            ),
        }
    )
    if empty_window:
        observation["data_status"] = "reachable"
        observation["notes"] = ["The stream connected but emitted no game update in the bounded scan window."]
    return observation


class PolymarketSportsWebSocketAdapter:
    info = AdapterInfo(
        adapter_id="polymarket_sports_websocket",
        venue="POLYMARKET",
        market_type="sports_event_state",
        source="Polymarket official anonymous live-sports WebSocket",
        capabilities=(
            "live_score",
            "game_status",
            "period",
            "elapsed_time",
            "possession",
            "sports_event_state",
            "websocket",
            "source_health",
        ),
        aliases=(
            "polymarket",
            "polymarket sports",
            "polymarket sports websocket",
            "nfl nhl mlb nba cbb cfb soccer esports tennis",
        ),
        docs_url=DOCS_URL,
        runtime_entrypoint="adapters.venues.polymarket.PolymarketSportsWebSocketAdapter",
        default_cache_minutes=0,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = _adapter_config(settings or {}, self.info.adapter_id)
        result = fetch_sports_messages(
            connect_timeout=max(0.1, float(cfg.get("connect_timeout_seconds", 8.0))),
            listen_seconds=max(0.0, float(cfg.get("listen_seconds", 6.0))),
            max_messages=max(1, min(int(cfg.get("max_messages", 100)), 1000)),
        )
        parser_failures: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []

        if result.get("ok"):
            for index, frame in enumerate(result.get("messages") or []):
                try:
                    observation = parse_polymarket_sports_message(
                        frame,
                        received_at=result.get("received_at"),
                        stale_after_seconds=max(
                            0.0, float(cfg.get("stale_after_seconds", 30.0))
                        ),
                    )
                    observation.update(
                        {
                            "fetch_status": str(result.get("status") or "reachable"),
                            "http_status": result.get("http_status"),
                            "connection_state": result.get("connection_state"),
                            "fetch_latency_ms": result.get("latency_ms"),
                        }
                    )
                    observations.append(observation)
                except (PolymarketSportsParseError, TypeError, ValueError) as exc:
                    message = f"Polymarket sports frame parser failed: {exc}"[:300]
                    parser_failures.append(
                        {"message_index": index, "source_url": SPORTS_WS_URL, "error": message}
                    )
            if not observations:
                parser_error = parser_failures[0]["error"] if parser_failures else None
                observations = [
                    _failure_observation(
                        result,
                        parser_error=parser_error,
                        empty_window=not parser_failures,
                    )
                ]
        else:
            observations = [_failure_observation(result)]

        source_status = str(result.get("status") or "unavailable")
        if parser_failures:
            source_status = "degraded"
        real_count = sum(1 for row in observations if row.get("score") is not None)
        freshness_states = {str(row.get("freshness_state") or "unknown") for row in observations}
        freshness_state = (
            "fresh"
            if "fresh" in freshness_states
            else "stale"
            if "stale" in freshness_states
            else "unknown"
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "adapter_spec_id": 616,
                "source_status": source_status,
                "source_urls": [SPORTS_WS_URL],
                "documentation_urls": [DOCS_URL, API_REFERENCE_URL],
                "fetch_status": {
                    "sports_stream": {
                        "source_url": SPORTS_WS_URL,
                        "fetch_status": str(result.get("status") or "unavailable"),
                        "http_status": result.get("http_status"),
                        "fetched_at": result.get("received_at"),
                        "latency_ms": result.get("latency_ms"),
                        "connection_state": result.get("connection_state"),
                        "heartbeat_count": result.get("heartbeat_count", 0),
                        "message_count": len(result.get("messages") or []),
                    }
                },
                "freshness_state": freshness_state,
                "freshness_states": sorted(freshness_states),
                "session_state": sorted(
                    {str(row.get("session_status") or "unknown") for row in observations}
                ),
                "parser_failures": parser_failures,
                "supported_sport_families": [
                    "american_football",
                    "baseball",
                    "basketball",
                    "esports",
                    "ice_hockey",
                    "soccer",
                    "tennis",
                ],
                "observation_count": len(observations),
                "real_observation_count": real_count,
                "candidate_count": 0,
                "watch_only": True,
                "paper_only": True,
            },
        )


register_adapter(PolymarketSportsWebSocketAdapter())
