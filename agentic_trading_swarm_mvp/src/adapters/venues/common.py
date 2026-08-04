"""Small dependency-free helpers shared by official public-market plugins."""

from __future__ import annotations

import datetime as dt
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any


def _system_trust_context() -> ssl.SSLContext | None:
    try:
        import truststore
    except ImportError:
        return None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _is_certificate_verification_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        reason, ssl.SSLCertVerificationError
    ) or "CERTIFICATE_VERIFY_FAILED" in str(exc).upper()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fetch_text(
    url: str,
    timeout: int = 15,
    *,
    method: str = "GET",
    json_body: Any | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    body = None if json_body is None else json.dumps(json_body, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
        "User-Agent": "agentic-trading-swarm-paper-research/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=str(method or "GET").upper(),
    )

    def _fetch(context: ssl.SSLContext | None = None) -> dict[str, Any]:
        kwargs = {"timeout": timeout}
        if context is not None:
            kwargs["context"] = context
        with urllib.request.urlopen(request, **kwargs) as response:
            text = response.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": "reachable",
                "http_status": int(getattr(response, "status", 200)),
                "text": text,
                "received_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "tls_trust_source": "system" if context is not None else "python_default",
            }

    try:
        return _fetch()
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": "blocked" if exc.code in {401, 403, 451} else "unavailable",
            "http_status": int(exc.code),
            "error": str(exc)[:300],
            "text": "",
            "received_at": utc_now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    except Exception as exc:  # noqa: BLE001 - scanner health must survive source outages.
        if _is_certificate_verification_error(exc):
            context = _system_trust_context()
            if context is not None:
                try:
                    return _fetch(context)
                except urllib.error.HTTPError as retry_exc:
                    return {
                        "ok": False,
                        "status": "blocked" if retry_exc.code in {401, 403, 451} else "unavailable",
                        "http_status": int(retry_exc.code),
                        "error": str(retry_exc)[:300],
                        "text": "",
                        "received_at": utc_now(),
                        "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                        "tls_trust_source": "system",
                    }
                except Exception as retry_exc:  # noqa: BLE001 - retain source health evidence.
                    exc = retry_exc
        return {
            "ok": False,
            "status": "unavailable",
            "http_status": None,
            "error": str(exc)[:300],
            "text": "",
            "received_at": utc_now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }


def fetch_bytes(url: str, timeout: int = 15, *, max_bytes: int = 10_000_000) -> dict[str, Any]:
    """Fetch a bounded public binary document while retaining source-health evidence."""

    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream,*/*;q=0.5",
            "User-Agent": "agentic-trading-swarm-paper-research/1.0",
        },
        method="GET",
    )

    def _fetch(context: ssl.SSLContext | None = None) -> dict[str, Any]:
        kwargs = {"timeout": timeout}
        if context is not None:
            kwargs["context"] = context
        with urllib.request.urlopen(request, **kwargs) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"public document exceeds {max_bytes} byte limit")
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError(f"public document exceeds {max_bytes} byte limit")
            return {
                "ok": True,
                "status": "reachable",
                "http_status": int(getattr(response, "status", 200)),
                "content": content,
                "content_type": response.headers.get_content_type(),
                "received_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "tls_trust_source": "system" if context is not None else "python_default",
            }

    try:
        return _fetch()
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": "blocked" if exc.code in {401, 403, 451} else "unavailable",
            "http_status": int(exc.code),
            "error": str(exc)[:300],
            "content": b"",
            "received_at": utc_now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    except Exception as exc:  # noqa: BLE001 - scanner health must survive source outages.
        if _is_certificate_verification_error(exc):
            context = _system_trust_context()
            if context is not None:
                try:
                    return _fetch(context)
                except urllib.error.HTTPError as retry_exc:
                    return {
                        "ok": False,
                        "status": "blocked" if retry_exc.code in {401, 403, 451} else "unavailable",
                        "http_status": int(retry_exc.code),
                        "error": str(retry_exc)[:300],
                        "content": b"",
                        "received_at": utc_now(),
                        "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                        "tls_trust_source": "system",
                    }
                except Exception as retry_exc:  # noqa: BLE001 - retain source health evidence.
                    exc = retry_exc
        return {
            "ok": False,
            "status": "unavailable",
            "http_status": None,
            "error": str(exc)[:300],
            "content": b"",
            "received_at": utc_now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }


def parse_json(text: str) -> Any:
    return json.loads(text)


def number(value: Any) -> float | None:
    text = str(value or "").strip().replace("\u00a0", " ")
    if not text or text in {"-", "–", "—", "n.s", "n.s.", "N/A", "n.a."}:
        return None
    text = text.replace(" ", "").replace(",", "." if text.count(",") == 1 and "." not in text else "")
    text = re.sub(r"[^0-9.+-]", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif self._table is not None and tag == "tr":
            self._row = []
        elif self._row is not None and tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def html_tables(text: str) -> list[list[list[str]]]:
    parser = TableParser()
    parser.feed(text)
    return parser.tables


def health_observation(venue: str, source_url: str, result: dict[str, Any], surface: str) -> dict[str, Any]:
    return {
        "venue": venue,
        "inst_id": f"{venue}:ADAPTER_HEALTH",
        "instrument_id": f"{venue}:ADAPTER_HEALTH",
        "symbol": "ADAPTER_HEALTH",
        "base": "ADAPTER_HEALTH",
        "quote": "N/A",
        "market_type": "reference",
        "market_surface": surface,
        "asset_class": "market_data_health",
        "trade_type": "official_market_reference",
        "direction": "watch_only",
        "last": 0.0,
        "data_status": result.get("status", "unavailable"),
        "http_status": result.get("http_status"),
        "observed_at": result.get("received_at") or utc_now(),
        "session_status": "unknown",
        "source_url": source_url,
        "latency_ms": result.get("latency_ms"),
        "candidate_reject_reason": "public_reference_source_unavailable",
        "notes": [str(result.get("error") or "Public reference source did not return usable data.")],
    }
