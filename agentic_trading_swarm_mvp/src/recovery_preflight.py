"""Fail-closed preflight for the tracked bounded crypto paper profile."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3

from settings import SettingsError, config_fingerprint, load_settings


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_RADAR_DB_PATH = ROOT / "runs" / "radar.sqlite"
DEFAULT_CODEX_DB_PATH = ROOT / "runs" / "codex_coordination.sqlite"
BOUNDED_RECOVERY_PROFILE = "bounded_crypto_paper_v1"
BOUNDED_RECOVERY_CAMPAIGN_ID = "bounded_crypto_paper_v1"


PROVIDER_KEY_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_API_KEY",
    "COHERE_API_KEY",
    "CODEX_API_KEY",
)

FORBIDDEN_WORKER_COMMAND_MARKERS = (
    "system_watchdog.ps1",
    "start_system_hidden.ps1",
    "run_radar_forever.ps1",
    "run_evolution_worker_forever.ps1",
    "evolution_worker.py",
    "run_codex_worker_pool_forever.ps1",
    "codex_worker_pool.py",
    "research_worker.py",
    "run_paid_research_once.ps1",
    "paid_research_once.py",
    "adapter_implementation_owner.py",
    "market_activation_owner.py",
    "strategy_implementation_owner.py",
    "autonomous_builder.py",
    "code_evolution.py",
)


def forbidden_worker_marker(command_line: str) -> str | None:
    normalized = str(command_line or "").replace("/", "\\").lower()
    for marker in FORBIDDEN_WORKER_COMMAND_MARKERS:
        if marker.lower() in normalized:
            return marker
    return None


def _parse_time(value: object) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _read_only_connection(path: pathlib.Path) -> sqlite3.Connection | None:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return None
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone() is not None


def _lease_counts(rows: list[sqlite3.Row], now: dt.datetime) -> tuple[int, int]:
    active = 0
    stale = 0
    for row in rows:
        expiry = _parse_time(row["lease_expires_at"])
        if expiry is not None and expiry > now:
            active += 1
        else:
            stale += 1
    return active, stale


def inspect_persisted_worker_claims(
    *,
    radar_db_path: pathlib.Path = DEFAULT_RADAR_DB_PATH,
    codex_db_path: pathlib.Path = DEFAULT_CODEX_DB_PATH,
    now: dt.datetime | None = None,
) -> dict:
    """Classify fresh worker leases as active and expired/malformed ones as stale."""

    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    detail: dict[str, dict[str, int]] = {}
    active_total = 0
    stale_total = 0
    radar = _read_only_connection(pathlib.Path(radar_db_path))
    if radar is not None:
        try:
            for table, predicate in (
                ("strategy_owner_tasks", "claimed_pid is not null"),
                ("market_activation_tasks", "claimed_pid is not null"),
            ):
                if not _table_exists(radar, table):
                    continue
                rows = radar.execute(
                    f"select lease_expires_at from {table} where {predicate}"
                ).fetchall()
                active, stale = _lease_counts(rows, now)
                detail[table] = {"active": active, "stale": stale}
                active_total += active
                stale_total += stale
            if _table_exists(radar, "paper_expansion_campaign_state"):
                paid_active = 0
                paid_stale = 0
                for row in radar.execute(
                    "select state_json from paper_expansion_campaign_state"
                ).fetchall():
                    try:
                        state = json.loads(row["state_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        state = {}
                    lease = state.get("paid_research_inflight") if isinstance(state, dict) else None
                    if not isinstance(lease, dict):
                        continue
                    expiry = _parse_time(lease.get("lease_expires_at"))
                    if expiry is not None and expiry > now:
                        paid_active += 1
                    else:
                        paid_stale += 1
                detail["paid_research_inflight"] = {
                    "active": paid_active,
                    "stale": paid_stale,
                }
                active_total += paid_active
                stale_total += paid_stale
        finally:
            radar.close()
    codex = _read_only_connection(pathlib.Path(codex_db_path))
    if codex is not None:
        try:
            for table, predicate in (
                ("codex_tasks", "claimed_by is not null"),
                ("codex_verification_jobs", "claimed_by is not null"),
                ("codex_resource_leases", "owner_worker_id is not null"),
            ):
                if not _table_exists(codex, table):
                    continue
                rows = codex.execute(
                    f"select lease_expires_at from {table} where {predicate}"
                ).fetchall()
                active, stale = _lease_counts(rows, now)
                detail[table] = {"active": active, "stale": stale}
                active_total += active
                stale_total += stale
        finally:
            codex.close()
    return {
        "active_forbidden_claims": active_total,
        "stale_ignored_claims": stale_total,
        "detail": detail,
    }


def validate_process_lock() -> None:
    truthy = {"1", "true", "yes", "locked", "disabled"}
    for name in ("RADAR_MODEL_CREDENTIAL_LOCK", "RADAR_MODELS_DISABLED"):
        if str(os.environ.get(name) or "").strip().lower() not in truthy:
            raise SettingsError(f"required process credential lock is missing: {name}")
    if os.environ.get("RADAR_USE_LITELLM") == "1":
        raise SettingsError("RADAR_USE_LITELLM must not enable provider calls")
    present = [name for name in PROVIDER_KEY_NAMES if os.environ.get(name)]
    if present:
        raise SettingsError("provider credentials remain in radar process: " + ",".join(present))
    if os.environ.get("RADAR_RESEARCH_MODEL_OVERRIDE"):
        raise SettingsError("research model override is forbidden in the radar process")


def validate_bounded_recovery_preflight_settings(settings: dict) -> None:
    """Reject every explicit config that is not the tracked bounded campaign."""

    operations = settings.get("operations") or {}
    expansion = settings.get("paper_expansion") or {}
    errors: list[str] = []
    if operations.get("fail_closed_recovery_profile") is not True:
        errors.append("operations.fail_closed_recovery_profile_must_be_true")
    if operations.get("profile") != BOUNDED_RECOVERY_PROFILE:
        errors.append(
            f"operations.profile_must_equal_{BOUNDED_RECOVERY_PROFILE}"
        )
    if expansion.get("enabled") is not True:
        errors.append("paper_expansion.enabled_must_be_true")
    if expansion.get("campaign_id") != BOUNDED_RECOVERY_CAMPAIGN_ID:
        errors.append(
            f"paper_expansion.campaign_id_must_equal_{BOUNDED_RECOVERY_CAMPAIGN_ID}"
        )
    if settings.get("mode") != "paper":
        errors.append("mode_must_be_paper")
    if settings.get("allow_live_trading") is not False:
        errors.append("allow_live_trading_must_be_false")
    if operations.get("crypto_only") is not True:
        errors.append("operations.crypto_only_must_be_true")
    if operations.get("model_credentials_enabled") is not False:
        errors.append("operations.model_credentials_enabled_must_be_false")
    if errors:
        raise SettingsError(
            "bounded recovery preflight rejected configuration: " + "; ".join(errors)
        )


def run_preflight(
    config_path: str | pathlib.Path,
    *,
    require_process_lock: bool = False,
    radar_db_path: pathlib.Path = DEFAULT_RADAR_DB_PATH,
    codex_db_path: pathlib.Path = DEFAULT_CODEX_DB_PATH,
) -> dict:
    path = pathlib.Path(config_path).expanduser().resolve()
    settings = load_settings(path, require_explicit=True)
    validate_bounded_recovery_preflight_settings(settings)
    if require_process_lock:
        validate_process_lock()
    claims = inspect_persisted_worker_claims(
        radar_db_path=radar_db_path,
        codex_db_path=codex_db_path,
    )
    if int(claims["active_forbidden_claims"]) != 0:
        raise SettingsError("active forbidden worker claims remain")
    return {
        "status": "ready",
        "config_path": str(path),
        "config_sha256": config_fingerprint(path),
        "profile": str((settings.get("operations") or {}).get("profile") or ""),
        "campaign_id": str((settings.get("paper_expansion") or {}).get("campaign_id") or ""),
        "mode": settings.get("mode"),
        "allow_live_trading": bool(settings.get("allow_live_trading", False)),
        "paper_notional_usd": float((settings.get("risk") or {}).get("paper_notional_usd", 0.0)),
        "model_credentials_enabled": bool(
            (settings.get("operations") or {}).get("model_credentials_enabled", True)
        ),
        "worker_claims": claims,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Explicit tracked recovery settings JSON")
    parser.add_argument("--require-process-lock", action="store_true")
    args = parser.parse_args()
    try:
        report = run_preflight(args.config, require_process_lock=args.require_process_lock)
    except (FileNotFoundError, OSError, SettingsError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
