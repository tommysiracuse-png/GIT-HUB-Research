"""Durable market-onboarding state and precise stall diagnostics.

Data-provider health, strategy evidence, and route feasibility are deliberately
separate here. A blocked endpoint must not become a losing-signal label, and a
losing strategy must not quarantine every strategy that shares its data feed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections import Counter
from typing import Any

from proxy_signal_quality import proxy_short_quality_review
from storage import RUNS_DIR, reliable_paper_label_eligibility_for_trade_row, signal_key, utc_now


REPORT_JSON = RUNS_DIR / "market_admission_report.json"
REPORT_MD = RUNS_DIR / "market_admission_report.md"
STAGES = (
    "discovered",
    "reachable",
    "normalized",
    "priceable",
    "quality_verified",
    "strategy_candidate",
    "route_feasible",
    "paper_eligible",
    "paper_evaluated",
)
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
ACTIVE_SESSION_STATES = {"open", "continuous", "unknown"}
ROUTE_OK_STATES = {"standard", "conditional", "feasible", "paper_proxy"}
_EVIDENCE_FIELDS = (
    "venue",
    "inst_id",
    "instrument_id",
    "market_surface",
    "proxy_surface",
    "trade_type",
    "direction",
    "data_status",
    "http_status",
    "session_status",
    "market_session_status",
    "last",
    "bid",
    "ask",
    "mark_price",
    "index_price",
    "funding_rate",
    "quality_status",
    "proxy_quality_status",
    "quote_normalization_status",
    "freshness_state",
    "liquidity_score",
    "spread_bps",
    "candidate_reject_reason",
)
_SOURCE_TIME_FIELDS = (
    "book_timestamp",
    "exchange_timestamp",
    "source_timestamp",
    "ticker_timestamp",
    "source_observed_at",
    "book_observed_at",
)
_REFERENCE_INVENTORY_TOKENS = (
    "reference_only",
    "reference_static",
    "not_executable_quote",
    "not_order_routable",
    "official_market_catalog",
    "static_register",
    "instrument_register",
    "contract_register",
    "security_directory",
)
_DORMANT_CONFIG_TOKENS = (
    "network_region_blocked",
    "region_blocked",
    "capability_missing",
    "missing_capability",
    "capability_unavailable",
    "configuration_missing",
    "config_missing",
    "credentials_missing",
    "credential_missing",
    "api_key_missing",
    "permission_missing",
)


# Identity v1 omitted direction whenever a scanner supplied an explicit
# lineage.  Keep the old hash computable for audit/migration tooling, but issue
# every new identity under a direction-aware version so opposing trades cannot
# share state or an admission episode.
ADMISSION_IDENTITY_VERSION = 2


def _market_admission_cfg(settings: dict) -> dict:
    cfg = settings.get("market_admission") or {}
    return cfg if isinstance(cfg, dict) else {}


def market_admission_monitor_enabled(settings: dict) -> bool:
    cfg = _market_admission_cfg(settings)
    return bool(cfg.get("enabled", True)) and bool(cfg.get("monitor_enabled", True))


def paper_admission_queue_enabled(settings: dict) -> bool:
    cfg = _market_admission_cfg(settings)
    return bool(cfg.get("enabled", True)) and bool(
        cfg.get("paper_queue_enabled", cfg.get("queue_enabled", False))
    )


def market_admission_bridge_enabled(settings: dict) -> bool:
    cfg = _market_admission_cfg(settings)
    return bool(cfg.get("enabled", True)) and bool(cfg.get("bridge_enabled", True))


def market_admission_diagnostics_enabled(settings: dict) -> bool:
    cfg = _market_admission_cfg(settings)
    return bool(cfg.get("enabled", True)) and bool(
        cfg.get("diagnostics_enabled", cfg.get("actions_enabled", True))
    )


def _source_observation_at(item: dict, fallback: str) -> str:
    for key in _SOURCE_TIME_FIELDS:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    cache_status = str(item.get("cache_status") or "").strip().lower()
    if cache_status not in {"cached", "stale", "fallback_cache"}:
        value = str(item.get("observed_at") or "").strip()
        if value:
            return value
    return fallback


def admission_evidence_fingerprint(item: dict) -> str:
    """Hash market evidence while ignoring loop-local orchestration metadata."""

    payload = {key: item.get(key) for key in _EVIDENCE_FIELDS if key in item}
    payload.update(
        {
            "surface": _surface(item),
            "lineage": _lineage(item),
            "route_status": _route_status(item),
            "source_timestamp": next(
                (str(item.get(key)) for key in _SOURCE_TIME_FIELDS if item.get(key) not in (None, "")),
                None,
            ),
        }
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text or default


def _route_status(candidate: dict) -> str:
    for container in (
        candidate,
        candidate.get("execution_route"),
        candidate.get("execution_feasibility"),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("route_status", "status", "paper_route_status"):
            value = str(container.get(key) or "").strip().lower()
            if value:
                return value
    return "unknown"


def _data_source(item: dict) -> str:
    source = item.get("data_source")
    if isinstance(source, dict):
        return _text(source.get("provider") or source.get("source_url") or source.get("url"))
    return _text(item.get("price_source") or item.get("source_url") or source)


def _surface(item: dict) -> str:
    return _text(
        item.get("proxy_surface")
        or item.get("market_surface")
        or item.get("surface_type_classified")
        or item.get("trade_type")
    )


def _lineage(item: dict) -> str:
    if item.get("signal_lineage_key"):
        return _text(item.get("signal_lineage_key"))
    if item.get("strategy_lab_id"):
        return f"STRATEGY_LAB|{item['strategy_lab_id']}|v{item.get('strategy_lab_version', 1)}"
    if item.get("signal_variant_id"):
        return _text(item.get("signal_variant_id"))
    direction = str(item.get("direction") or "").strip().lower()
    if direction == "watch_only":
        strategy_markers = (
            item.get("score"),
            item.get("edge_bps_estimate"),
            item.get("execution_feasibility"),
            item.get("execution_route"),
            item.get("proxy_surface"),
        )
        if all(marker in (None, "", [], {}, ()) for marker in strategy_markers):
            return "adapter_observation"
    if item.get("direction") and item.get("trade_type"):
        return f"{item.get('trade_type')}|{item.get('direction')}"
    return "adapter_observation"


def _normalized_direction(item: dict) -> str:
    raw = str(item.get("direction") or "").strip().lower()
    normalized = "_".join(part for part in re.split(r"[^a-z0-9]+", raw) if part)
    return normalized or "unspecified"


def _legacy_admission_key_v1(item: dict) -> str:
    identity = "|".join(
        (
            _text(item.get("venue")).upper(),
            _text(item.get("inst_id") or item.get("instrument_id")),
            _surface(item),
            _lineage(item),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def _admission_key(item: dict) -> str:
    identity = "|".join(
        (
            f"v{ADMISSION_IDENTITY_VERSION}",
            _text(item.get("venue")).upper(),
            _text(item.get("inst_id") or item.get("instrument_id")),
            _surface(item),
            _lineage(item),
            _normalized_direction(item),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def _admission_episode_id(item: dict) -> str:
    metadata = item.get("paper_admission")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(
        item.get("admission_episode_id")
        or item.get("episode_id")
        or metadata.get("episode_id")
        or ""
    ).strip()


def admission_key_for(item: dict) -> str:
    return _admission_key(item)


def admission_lineage_for(item: dict) -> str:
    return _lineage(item)


def admission_surface_for(item: dict) -> str:
    return _surface(item)


def admission_identity_audit_for(item: dict) -> dict[str, Any]:
    """Return the versioned identity plus its legacy-v1 audit linkage."""

    return {
        "identity_version": ADMISSION_IDENTITY_VERSION,
        "normalized_direction": _normalized_direction(item),
        "legacy_v1_admission_key": _legacy_admission_key_v1(item),
    }


def admission_terminal_class_for(item: dict) -> str | None:
    """Identify finite reference inventory that must never retry as a trade."""

    if any(
        item.get(key)
        for key in (
            "paper_only_reference",
            "reference_only",
            "static_reference",
            "reference_inventory",
        )
    ):
        return "terminal_reference"
    freshness = str(item.get("freshness_state") or "").strip().lower()
    if freshness in {"reference_static", "static_reference"}:
        return "terminal_reference"
    semantics = "|".join(
        str(item.get(key) or "").strip().lower()
        for key in (
            "trade_type",
            "market_type",
            "market_surface",
            "surface_type_classified",
            "candidate_reject_reason",
            "execution_semantics",
            "price_reference_role",
        )
    )
    if any(token in semantics for token in _REFERENCE_INVENTORY_TOKENS):
        return "terminal_reference"
    direction = str(item.get("direction") or "").strip().lower()
    if direction in {"", "watch_only"} and any(
        token in semantics for token in ("reference", "catalog", "register", "directory")
    ):
        return "terminal_reference"
    return None


def admission_operational_class_for(item: dict) -> str | None:
    terminal = admission_terminal_class_for(item)
    if terminal:
        return terminal
    if _session_status(item) == "closed":
        return "scheduled_wait"
    blocker_text = "|".join(
        str(item.get(key) or "").strip().lower()
        for key in (
            "access_blocker_code",
            "candidate_reject_reason",
            "capability_status",
            "configuration_status",
            "route_capability_status",
        )
    )
    data_status = str(item.get("data_status") or "").strip().lower()
    http_status = str(item.get("http_status") or "").strip().lower()
    if any(token in blocker_text for token in _DORMANT_CONFIG_TOKENS) or (
        data_status == "blocked" and ("403" in http_status or "451" in http_status)
    ):
        return "dormant_until_config_change"
    return None


def _session_status(item: dict) -> str:
    value = _text(item.get("session_status") or item.get("market_session_status"), "unknown").lower()
    return value if value in {"open", "closed", "continuous", "unknown"} else "unknown"


def _is_normalized(item: dict) -> bool:
    if item.get("proxy_symbol"):
        return bool(item.get("venue") and item.get("inst_id"))
    return bool(
        item.get("venue")
        and (item.get("inst_id") or item.get("instrument_id"))
        and (
            item.get("base")
            or item.get("base_asset")
            or item.get("underlying")
            or item.get("asset_class")
            or item.get("market_surface")
        )
    )


def _quality_verified(item: dict, config: dict | None = None) -> bool:
    proxy_short_review = proxy_short_quality_review(item, config)
    if proxy_short_review["applies"] and not proxy_short_review["eligible"]:
        return False
    quality = str(item.get("quality_status") or item.get("proxy_quality_status") or "").lower()
    if quality in {"verified", "verified_proxy", "normal"}:
        return True
    if item.get("proxy_symbol"):
        return (
            _session_status(item) in ACTIVE_SESSION_STATES
            and float(item.get("stale_minutes") or 0.0) <= float(item.get("max_entry_stale_minutes") or 90.0)
            and float(item.get("liquidity_score") or 0.0) > 0.0
        )
    return False


def _blocker(item: dict, review: dict | None, stage: str, config: dict | None = None) -> str | None:
    session = _session_status(item)
    if session == "closed":
        return "market_closed"
    data_status = str(item.get("data_status") or "reachable").lower()
    http_status = str(item.get("http_status") or "").lower()
    notes = " ".join(str(value) for value in item.get("notes") or []).lower()
    if item.get("access_blocker_code") == "network_region_blocked" or (
        data_status == "blocked" and ("403" in http_status or "451" in http_status or "probe_deferred" in http_status)
    ):
        return "network_region_blocked"
    if "parser failed" in notes or "parser" in str(item.get("candidate_reject_reason") or "").lower():
        return "parser_failure"
    if data_status in {"blocked", "unavailable", "degraded"}:
        return f"data_{data_status}"
    if stage == "reachable":
        return "normalization_missing"
    if stage == "normalized":
        return "price_missing"
    normalization = str(item.get("quote_normalization_status") or "").lower()
    if normalization.startswith("missing") or normalization == "unsupported_quote":
        return "missing_quote_normalization"
    if stage == "priceable" and not _quality_verified(item, config):
        proxy_short_review = proxy_short_quality_review(item, config)
        if proxy_short_review["applies"] and proxy_short_review["quality_failure_reason"]:
            return str(proxy_short_review["quality_failure_reason"])
        return str(item.get("regional_candidate_gate_status") or item.get("quality_action") or "quality_unverified")
    reason = str(item.get("candidate_reject_reason") or "").strip()
    if stage == "quality_verified" and (item.get("direction") in {None, "", "watch_only"}):
        return reason or "no_candidate_logic"
    route_status = _route_status(item)
    if stage == "strategy_candidate" and route_status not in ROUTE_OK_STATES:
        return f"route_{route_status}"
    if review and stage == "route_feasible":
        blocks = review.get("hard_blocks") or []
        return str(blocks[0]) if blocks else "paper_review_not_approved"
    return reason or None


def _stage_for(item: dict, review: dict | None, stats: dict, config: dict | None = None) -> str:
    stage = "discovered"
    data_status = str(item.get("data_status") or "reachable").lower()
    if data_status != "reachable":
        return stage
    stage = "reachable"
    if not _is_normalized(item):
        return stage
    stage = "normalized"
    if float(item.get("last") or 0.0) <= 0.0:
        return stage
    stage = "priceable"
    if not _quality_verified(item, config):
        return stage
    stage = "quality_verified"
    if item.get("direction") in {None, "", "watch_only"}:
        return stage
    stage = "strategy_candidate"
    if _route_status(item) not in ROUTE_OK_STATES:
        return stage
    stage = "route_feasible"
    if not review or review.get("decision") not in {"approve_paper_trade", "approve_conditional_paper_trade"}:
        return stage
    stage = "paper_eligible"
    if (
        stats.get("attribution_scope") == "admission_episode"
        and int(stats.get("valid_labels") or 0) > 0
    ):
        return "paper_evaluated"
    return stage


def _empty_paper_stats() -> dict:
    return {"trades": 0, "closed_trades": 0, "valid_labels": 0, "avg_pnl_bps": None}


def _paper_stats(
    conn: sqlite3.Connection,
    *,
    require_queue_lineage: bool = False,
) -> dict[str, dict]:
    """Build exact attribution indexes; never borrow results from another signal on the instrument."""

    output: dict[str, dict] = {
        "by_admission_episode": {},
        "by_admission": {},
        "by_identity_signal": {},
        "by_signal": {},
    }
    columns = {str(row["name"]) for row in conn.execute("pragma table_info(paper_trades)").fetchall()}
    admission_expr = "admission_key" if "admission_key" in columns else "null as admission_key"
    episode_expr = "admission_episode_id" if "admission_episode_id" in columns else "null as admission_episode_id"
    measurement_expr = (
        "close_measurement_status"
        if "close_measurement_status" in columns
        else "null as close_measurement_status"
    )
    joined_measurement_expr = (
        "p.close_measurement_status"
        if "close_measurement_status" in columns
        else "null as close_measurement_status"
    )
    joined_admission_expr = "p.admission_key" if "admission_key" in columns else "null as admission_key"
    joined_episode_expr = (
        "p.admission_episode_id"
        if "admission_episode_id" in columns
        else "null as admission_episode_id"
    )
    rows = conn.execute(
        f"""
        select id, inst_id, signal_key, status, pnl_bps, candidate_json,
               review_json, context_json, {measurement_expr}, {admission_expr}, {episode_expr}
        from paper_trades
        """
    ).fetchall()

    def keys_for(row: sqlite3.Row) -> list[tuple[str, object]]:
        keys: list[tuple[str, object]] = []
        admission_key = str(row["admission_key"] or "").strip()
        episode_id = str(row["admission_episode_id"] or "").strip()
        if admission_key and episode_id:
            keys.append(("by_admission_episode", (admission_key, episode_id)))
        if admission_key:
            keys.append(("by_admission", admission_key))
        inst_id = str(row["inst_id"] or "").strip()
        stored_signal = str(row["signal_key"] or "").strip()
        if inst_id and stored_signal:
            keys.append(("by_signal", (inst_id, stored_signal)))
        try:
            stored_candidate = json.loads(row["candidate_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_candidate = {}
        if (
            isinstance(stored_candidate, dict)
            and stored_candidate.get("venue")
            and (stored_candidate.get("inst_id") or stored_candidate.get("instrument_id"))
            and stored_signal
        ):
            keys.append(
                (
                    "by_identity_signal",
                    (_admission_key(stored_candidate), stored_signal),
                )
            )
        return keys

    for row in rows:
        exact_keys = keys_for(row)
        for index_name, key in exact_keys:
            stats = output[index_name].setdefault(key, {**_empty_paper_stats(), "_pnl_sum": 0.0})
            stats["trades"] += 1
        if str(row["status"] or "").strip().lower() != "closed":
            continue
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        try:
            pnl_bps = float(row["pnl_bps"])
        except (TypeError, ValueError):
            continue
        for index_name, key in exact_keys:
            stats = output[index_name][key]
            stats["closed_trades"] += 1
            stats["_pnl_sum"] += pnl_bps

    for index in output.values():
        for stats in index.values():
            if stats["closed_trades"]:
                stats["avg_pnl_bps"] = stats["_pnl_sum"] / stats["closed_trades"]
            stats.pop("_pnl_sum", None)

    exact_queue_sql = ""
    if require_queue_lineage:
        exact_queue_sql = """
              and p.admission_key is not null
              and p.admission_episode_id is not null
              and exists (
                  select 1 from paper_admission_queue q
                  where q.paper_trade_id=p.id
                    and q.admission_key=p.admission_key
                    and q.episode_id=p.admission_episode_id
              )
        """
    try:
        labels = conn.execute(
            f"""
            select p.inst_id, p.signal_key, p.status, p.candidate_json,
                   p.review_json, p.context_json, {joined_measurement_expr},
                   {joined_admission_expr}, {joined_episode_expr},
                   count(*) as valid_label_count
            from paper_trade_outcomes o
            join paper_trades p on p.id = o.trade_id
            where o.measurement_status = 'valid' and p.status = 'closed'
              and (p.admission_key is null or o.admission_key = p.admission_key)
              and (p.admission_episode_id is null or o.admission_episode_id = p.admission_episode_id)
              {exact_queue_sql}
            group by p.id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        labels = []
    for row in labels:
        if not reliable_paper_label_eligibility_for_trade_row(row)["paper_label_eligible"]:
            continue
        for index_name, key in keys_for(row):
            stats = output[index_name].setdefault(key, _empty_paper_stats())
            stats["valid_labels"] = int(stats.get("valid_labels") or 0) + int(
                row["valid_label_count"] or 0
            )
    # Keep the established exact (instrument, signal-key) lookup available to
    # read-only consumers while the named indexes carry admission identities.
    # This is an exact-signal alias, not the removed same-instrument fallback.
    output.update(output["by_signal"])
    return output


def _stats_for(item: dict, stats: dict[str, dict]) -> dict:
    explicit_admission_key = str(item.get("admission_key") or "").strip()
    computed_admission_key = _admission_key(item)
    if explicit_admission_key and explicit_admission_key != computed_admission_key:
        return {**_empty_paper_stats(), "attribution_scope": "identity_mismatch"}
    admission_key = explicit_admission_key or computed_admission_key
    episode_id = _admission_episode_id(item)
    if admission_key and episode_id:
        exact = stats["by_admission_episode"].get((admission_key, episode_id))
        if exact is not None:
            return {**exact, "attribution_scope": "admission_episode"}
        # Once an episode is named, do not borrow an older episode or a
        # same-signal result. Queue reconciliation depends on this isolation.
        return {**_empty_paper_stats(), "attribution_scope": "none"}
    if admission_key:
        exact = stats["by_admission"].get(admission_key)
        if exact is not None:
            return {**exact, "attribution_scope": "admission"}
        if explicit_admission_key:
            return {**_empty_paper_stats(), "attribution_scope": "none"}
    if item.get("direction"):
        exact = stats["by_identity_signal"].get(
            (computed_admission_key, signal_key(item))
        )
        if exact is not None:
            return {**exact, "attribution_scope": "signal"}
    return {**_empty_paper_stats(), "attribution_scope": "none"}


def _cohort_key(state: dict) -> str:
    identity = "|".join(
        (
            _text(state.get("venue")).upper(),
            _text(state.get("current_stage")),
            _text(state.get("blocker_code")),
            _text(state.get("market_surface")),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _cohort_members(conn: sqlite3.Connection, state: dict) -> list[sqlite3.Row]:
    return conn.execute(
        """
        select admission_key, inst_id, stalled_eligible_scans
        from market_admission_states
        where venue = ? and current_stage = ? and coalesce(blocker_code, '') = ?
          and market_surface = ?
        order by stalled_eligible_scans desc, inst_id asc
        """,
        (
            _text(state.get("venue")).upper(),
            _text(state.get("current_stage")),
            str(state.get("blocker_code") or ""),
            _text(state.get("market_surface")),
        ),
    ).fetchall()


def _upsert_diagnostic(conn: sqlite3.Connection, state: dict, priority: int) -> None:
    cohort_key = _cohort_key(state)
    members = _cohort_members(conn, state)
    market_key = f"market_admission_cohort|{cohort_key}"
    directive = (
        f"Diagnose {state['venue']} {state.get('market_surface')} cohort stalled at "
        f"{state['current_stage']} by {state.get('blocker_code')}"
    )
    evidence = json.dumps(
        {
            "cohort_key": cohort_key,
            "stage": state["current_stage"],
            "blocker": state.get("blocker_code"),
            "market_surface": state.get("market_surface"),
            "instrument_count": len(members),
            "sample_instruments": [str(row["inst_id"]) for row in members[:20]],
            "max_stalled_eligible_scans": max(
                [int(row["stalled_eligible_scans"] or 0) for row in members],
                default=int(state["stalled_eligible_scans"]),
            ),
        },
        sort_keys=True,
    )
    existing = conn.execute(
        "select id from market_hunter_directives where market_key = ? and directive = ? limit 1",
        (market_key, directive),
    ).fetchone()
    if existing:
        conn.execute(
            "update market_hunter_directives set priority = ?, evidence_json = ?, status = 'open' where id = ?",
            (priority, evidence, int(existing["id"])),
        )
    else:
        conn.execute(
            """
            insert into market_hunter_directives
                (created_at, market_key, directive, priority, rationale, evidence_json, status)
            values (?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                utc_now(),
                market_key,
                directive,
                priority,
                "Advance the exact blocked admission stage; do not infer strategy failure from provider or route health.",
                evidence,
            ),
        )


def _upsert_task(conn: sqlite3.Connection, state: dict) -> None:
    cohort_key = _cohort_key(state)
    members = _cohort_members(conn, state)
    samples = [str(row["inst_id"]) for row in members[:20]]
    title = f"Market admission cohort stalled [{cohort_key}]"
    rationale = (
        f"{state['venue']} {state.get('market_surface')} has {len(members)} instruments stalled at "
        f"{state['current_stage']}. Exact blocker: {state.get('blocker_code')}. "
        f"Sample instruments: {', '.join(samples[:10])}. "
        "Fix only the owning admission stage and preserve independent strategy/route evidence."
    )
    conn.execute(
        """
        insert into improvement_tasks (created_at, priority, title, rationale, status)
        values (?, 95, ?, ?, 'open')
        on conflict(title) do update set priority = 95, rationale = excluded.rationale, status = 'open'
        """,
        (utc_now(), title, rationale),
    )
    # Preserve history while removing the pre-cohort one-task-per-instrument flood.
    conn.execute(
        """
        update improvement_tasks
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open' and title like 'Market admission stalled [%'
          and rationale like ? and rationale like ?
        """,
        (f"{state['venue']} %", f"%Exact blocker: {state.get('blocker_code')}%"),
    )


def _resolve_actions(conn: sqlite3.Connection, admission_key: str) -> None:
    conn.execute(
        "update improvement_tasks set status = 'resolved_market_admission_advanced' where title = ? and status = 'open'",
        (f"Market admission stalled [{admission_key}]",),
    )
    conn.execute(
        "update market_hunter_directives set status = 'resolved_market_admission_advanced' where market_key = ? and status = 'open'",
        (f"market_admission|{admission_key}",),
    )


def _reconcile_cohort_tasks(conn: sqlite3.Connection, diagnostic_after: int, task_after: int) -> dict:
    rows = conn.execute(
        """
        select venue, current_stage, blocker_code, market_surface, stalled_eligible_scans
        from market_admission_states
        where blocker_code is not null and stalled_eligible_scans >= ?
        """,
        (int(task_after),),
    ).fetchall()
    active_titles = {
        f"Market admission cohort stalled [{_cohort_key(dict(row))}]"
        for row in rows
    }
    directive_rows = conn.execute(
        """
        select venue, current_stage, blocker_code, market_surface, stalled_eligible_scans
        from market_admission_states
        where blocker_code is not null and stalled_eligible_scans >= ?
        """,
        (int(diagnostic_after),),
    ).fetchall()
    active_directive_keys = {
        f"market_admission_cohort|{_cohort_key(dict(row))}"
        for row in directive_rows
    }
    open_rows = conn.execute(
        "select id, title from improvement_tasks "
        "where status = 'open' and title like 'Market admission cohort stalled [%]%'"
    ).fetchall()
    resolved = 0
    for row in open_rows:
        if str(row["title"]) in active_titles:
            continue
        conn.execute(
            "update improvement_tasks set status = 'resolved_market_admission_advanced' where id = ?",
            (int(row["id"]),),
        )
        resolved += 1
    open_directives = conn.execute(
        "select id, market_key from market_hunter_directives "
        "where status = 'open' and market_key like 'market_admission_cohort|%'"
    ).fetchall()
    resolved_directives = 0
    for row in open_directives:
        if str(row["market_key"]) in active_directive_keys:
            continue
        conn.execute(
            "update market_hunter_directives set status = 'resolved_market_admission_advanced' where id = ?",
            (int(row["id"]),),
        )
        resolved_directives += 1
    legacy_tasks = conn.execute(
        """
        update improvement_tasks
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open'
          and title like 'Market admission stalled [%]'
          and title not like 'Market admission cohort stalled [%]'
        """
    ).rowcount
    legacy_directives = conn.execute(
        """
        update market_hunter_directives
        set status = 'superseded_by_market_admission_cohort'
        where status = 'open' and market_key like 'market_admission|%'
        """
    ).rowcount
    return {
        "active_cohorts": len(active_titles),
        "active_diagnostic_cohorts": len(active_directive_keys),
        "resolved_cohort_tasks": resolved,
        "resolved_cohort_directives": resolved_directives,
        "legacy_instrument_tasks_superseded": int(legacy_tasks),
        "legacy_instrument_directives_superseded": int(legacy_directives),
    }


def _persistent_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        select current_stage, highest_stage, health_status, blocker_code, terminal_class,
               count(*) as state_count
        from market_admission_states
        group by current_stage, highest_stage, health_status, blocker_code, terminal_class
        """
    ).fetchall()
    by_current: Counter[str] = Counter()
    by_highest: Counter[str] = Counter()
    by_health: Counter[str] = Counter()
    by_blocker: Counter[str] = Counter()
    by_terminal_class: Counter[str] = Counter()
    total = 0
    for row in rows:
        count = int(row["state_count"] or 0)
        total += count
        by_current[str(row["current_stage"])] += count
        by_highest[str(row["highest_stage"])] += count
        by_health[str(row["health_status"])] += count
        if row["blocker_code"]:
            by_blocker[str(row["blocker_code"])] += count
        if row["terminal_class"]:
            by_terminal_class[str(row["terminal_class"])] += count
    return {
        "state_count": total,
        "by_current_stage": dict(by_current),
        "by_highest_stage": dict(by_highest),
        "by_health": dict(by_health),
        "by_blocker": dict(by_blocker),
        "by_terminal_class": dict(by_terminal_class),
        "paper_eligible_count": sum(
            count
            for stage, count in by_current.items()
            if STAGE_INDEX.get(stage, -1) >= STAGE_INDEX["paper_eligible"]
        ),
        "paper_evaluated_count": int(by_current.get("paper_evaluated", 0)),
        "lifetime_paper_eligible_count": sum(
            count
            for stage, count in by_highest.items()
            if STAGE_INDEX.get(stage, -1) >= STAGE_INDEX["paper_eligible"]
        ),
        "lifetime_paper_evaluated_count": int(by_highest.get("paper_evaluated", 0)),
    }


def _write_report(
    conn: sqlite3.Connection,
    states: list[dict],
    settings: dict,
    *,
    extra_summary: dict | None = None,
) -> dict:
    by_stage = Counter(item["current_stage"] for item in states)
    by_highest_stage = Counter(item["highest_stage"] for item in states)
    by_health = Counter(item["health_status"] for item in states)
    by_blocker = Counter(item.get("blocker_code") for item in states if item.get("blocker_code"))
    by_quality_failure = Counter(
        item.get("details", {}).get("quality_failure_reason")
        for item in states
        if item.get("details", {}).get("quality_failure_reason")
    )
    by_terminal_class = Counter(
        item.get("terminal_class") for item in states if item.get("terminal_class")
    )
    requested = set((settings.get("market_admission") or {}).get("requested_symbols") or [])
    requested_rows = [item for item in states if str(item.get("inst_id") or "").split(":")[-1] in requested]
    persistent = _persistent_summary(conn)
    report_limit = max(0, int(_market_admission_cfg(settings).get("report_state_limit", 100)))
    sampled_states = sorted(
        states,
        key=lambda item: (
            -STAGE_INDEX.get(str(item.get("current_stage") or ""), -1),
            -STAGE_INDEX.get(str(item.get("highest_stage") or ""), -1),
            str(item.get("venue") or ""),
            str(item.get("inst_id") or ""),
            str(item.get("market_surface") or ""),
            str(item.get("admission_key") or ""),
        ),
    )[:report_limit]
    summary = {
        "generated_at": utc_now(),
        "enabled": True,
        "monitor_enabled": market_admission_monitor_enabled(settings),
        "paper_queue_enabled": paper_admission_queue_enabled(settings),
        "bridge_enabled": market_admission_bridge_enabled(settings),
        "diagnostics_enabled": market_admission_diagnostics_enabled(settings),
        "state_count": len(states),
        "touched_state_count": len(states),
        "persistent_state_count": persistent["state_count"],
        "reported_state_count": len(sampled_states),
        "states_truncated": max(0, len(states) - len(sampled_states)),
        "omitted_state_count": max(0, len(states) - len(sampled_states)),
        "by_stage": dict(by_stage),
        "by_highest_stage": dict(by_highest_stage),
        "by_health": dict(by_health),
        "by_blocker": dict(by_blocker),
        "by_quality_failure": dict(by_quality_failure),
        "by_terminal_class": dict(by_terminal_class),
        "requested_symbol_count": len(requested),
        "requested_symbols_observed": len({str(item["inst_id"]).split(":")[-1] for item in requested_rows}),
        "paper_eligible_count": sum(
            STAGE_INDEX[item["current_stage"]] >= STAGE_INDEX["paper_eligible"]
            for item in states
        ),
        "paper_evaluated_count": sum(
            item["current_stage"] == "paper_evaluated" for item in states
        ),
        "lifetime_paper_eligible_count": sum(
            STAGE_INDEX[item["highest_stage"]] >= STAGE_INDEX["paper_eligible"]
            for item in states
        ),
        "lifetime_paper_evaluated_count": sum(
            item["highest_stage"] == "paper_evaluated" for item in states
        ),
        "persistent_by_stage": persistent["by_current_stage"],
        "persistent_by_highest_stage": persistent["by_highest_stage"],
        "persistent_by_health": persistent["by_health"],
        "persistent_by_blocker": persistent["by_blocker"],
        "persistent_by_terminal_class": persistent["by_terminal_class"],
        "lifetime_by_highest_stage": persistent["by_highest_stage"],
        "lifetime_terminal_reference_count": int(
            persistent["by_terminal_class"].get("terminal_reference", 0)
        ),
        "persistent_paper_eligible_count": persistent["paper_eligible_count"],
        "persistent_paper_evaluated_count": persistent["paper_evaluated_count"],
        "persistent_lifetime_paper_eligible_count": persistent[
            "lifetime_paper_eligible_count"
        ],
        "persistent_lifetime_paper_evaluated_count": persistent[
            "lifetime_paper_evaluated_count"
        ],
    }
    if extra_summary:
        summary.update(extra_summary)
    sampled_requested_rows = [
        item
        for item in sampled_states
        if str(item.get("inst_id") or "").split(":")[-1] in requested
    ]
    artifact_payload = {
        "summary": summary,
        "requested_markets": sampled_requested_rows,
        "states": sampled_states,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Market Admission Report",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Touched states: `{summary['touched_state_count']}`",
        f"- Persistent states: `{summary['persistent_state_count']}`",
        f"- Reported state sample: `{summary['reported_state_count']}`",
        f"- By stage: `{summary['by_stage']}`",
        f"- By health: `{summary['by_health']}`",
        f"- By blocker: `{summary['by_blocker']}`",
        f"- Proxy-short quality failures: `{summary['by_quality_failure']}`",
        f"- Requested symbols observed: `{summary['requested_symbols_observed']}/{summary['requested_symbol_count']}`",
        "",
        "## Requested Markets",
        "",
    ]
    for item in sampled_requested_rows:
        lines.append(
            f"- `{item['venue']}:{item['inst_id']}` surface=`{item['market_surface']}` "
            f"admission=`{item['admission_key']}` episode=`{item.get('current_episode_id')}` "
            f"stage=`{item['current_stage']}` highest=`{item['highest_stage']}` "
            f"health=`{item['health_status']}` blocker=`{item.get('blocker_code')}` labels=`{item['details'].get('valid_labels', 0)}`"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "summary": summary,
        "requested_markets": sampled_requested_rows,
        "states": sampled_states,
        "omitted_state_count": summary["omitted_state_count"],
    }


def run_market_admission_monitor(
    conn: sqlite3.Connection,
    settings: dict,
    candidates: list[dict],
    reviewed: list[dict],
    observations: list[dict] | None = None,
) -> dict:
    """Persist current onboarding states and create stage-specific diagnostics."""

    cfg = _market_admission_cfg(settings)
    if not market_admission_monitor_enabled(settings):
        return {
            "summary": {
                "enabled": False,
                "monitor_enabled": False,
                "paper_queue_enabled": paper_admission_queue_enabled(settings),
                "bridge_enabled": market_admission_bridge_enabled(settings),
                "diagnostics_enabled": market_admission_diagnostics_enabled(settings),
            },
            "states": [],
        }
    diagnostics_enabled = market_admission_diagnostics_enabled(settings)
    review_by_identity = {
        (
            _admission_key(item["candidate"]),
            _admission_episode_id(item["candidate"]),
        ): item
        for item in reviewed
        if isinstance(item.get("candidate"), dict)
    }
    combined: dict[str, dict] = {}
    for item in observations or []:
        normalized = dict(item)
        if not normalized.get("inst_id"):
            normalized["inst_id"] = normalized.get("instrument_id")
        combined[_admission_key(normalized)] = normalized
    for item in candidates:
        combined[_admission_key(item)] = dict(item)
    # Reviewed candidates carry the queue's canonical admission episode tags.
    # Merge them last so a pre-review scanner copy cannot erase that identity.
    for entry in reviewed:
        reviewed_candidate = entry.get("candidate") if isinstance(entry, dict) else None
        if not isinstance(reviewed_candidate, dict):
            continue
        combined[_admission_key(reviewed_candidate)] = dict(reviewed_candidate)

    now = utc_now()
    paper_stats = _paper_stats(
        conn,
        require_queue_lineage=paper_admission_queue_enabled(settings),
    )
    degraded_after = int(cfg.get("consecutive_failures_degraded", 5))
    diagnostic_after = int(cfg.get("diagnostic_after_eligible_scans", 30))
    task_after = int(cfg.get("implementation_task_after_eligible_scans", 120))
    touched: list[str] = []
    for item in combined.values():
        if not item.get("inst_id") or not item.get("venue"):
            continue
        computed_admission_key = _admission_key(item)
        supplied_admission_key = str(item.get("admission_key") or "").strip()
        admission_identity_mismatch = bool(
            supplied_admission_key and supplied_admission_key != computed_admission_key
        )
        admission_key = (
            supplied_admission_key
            if supplied_admission_key and not admission_identity_mismatch
            else computed_admission_key
        )
        lineage = _lineage(item)
        episode_id = _admission_episode_id(item)
        review_entry = review_by_identity.get(
            (computed_admission_key, episode_id)
        ) or {}
        review = review_entry.get("review") if isinstance(review_entry, dict) else None
        review = review if isinstance(review, dict) else None
        stats = _stats_for(item, paper_stats)
        current_stage = _stage_for(item, review, stats, cfg)
        session_status = _session_status(item)
        blocker = _blocker(item, review, current_stage, cfg)
        operational_class = admission_operational_class_for(item)
        if operational_class == "terminal_reference":
            blocker = blocker or "terminal_reference_inventory"
        elif operational_class == "scheduled_wait":
            blocker = "market_closed"
        elif operational_class == "dormant_until_config_change":
            blocker = blocker or "configuration_or_capability_block"
        eligible = session_status in ACTIVE_SESSION_STATES
        previous = conn.execute(
            "select * from market_admission_states where admission_key = ?",
            (admission_key,),
        ).fetchone()
        previous_stage = str(previous["current_stage"]) if previous else "discovered"
        previous_highest = str(previous["highest_stage"]) if previous else "discovered"
        previous_health = str(previous["health_status"]) if previous else None
        evidence_fingerprint = admission_evidence_fingerprint(item)
        previous_fingerprint = str(previous["last_evidence_fingerprint"] or "") if previous else ""
        fresh_evidence = previous is None or evidence_fingerprint != previous_fingerprint
        fresh_increment = 1 if fresh_evidence else 0
        counter_eligible = eligible and operational_class is None
        attempt_increment = 1 if counter_eligible and fresh_evidence else 0
        eligible_increment = attempt_increment
        advanced = STAGE_INDEX[current_stage] > STAGE_INDEX[previous_highest]
        highest_stage = current_stage if advanced else previous_highest
        stalled = 0 if advanced else int(previous["stalled_eligible_scans"] or 0) if previous else 0
        if counter_eligible and fresh_evidence and not advanced and current_stage != "paper_evaluated":
            stalled += 1
        if operational_class is not None:
            stalled = 0
        consecutive_failures = 0
        if blocker and operational_class is None:
            previous_failures = int(previous["consecutive_failures"] or 0) if previous else 0
            consecutive_failures = previous_failures + fresh_increment
        if operational_class is not None:
            health_status = operational_class
        elif consecutive_failures >= degraded_after:
            health_status = "blocked" if current_stage == "discovered" else "degraded"
        elif blocker:
            health_status = "waiting"
        else:
            health_status = "healthy"
        proxy_short_review = proxy_short_quality_review(item, cfg)
        source_observation_at = (
            _source_observation_at(item, now)
            if fresh_evidence or not previous
            else str(previous["last_observation_at"] or previous["last_seen_at"] or now)
        )
        stage_entered_at = (
            now
            if not previous or current_stage != previous_stage
            else str(previous["stage_entered_at"] or previous["last_advanced_at"] or now)
        )
        episode_id = episode_id or None
        review_opportunity_id = review_entry.get("opportunity_id") if isinstance(review_entry, dict) else None
        last_paper_trade_id = item.get("paper_trade_id")
        terminal_class = (
            admission_terminal_class_for(item)
            or str(item.get("terminal_class") or "").strip()
            or None
        )
        details = {
            **stats,
            **admission_identity_audit_for(item),
            "adapter_id": item.get("adapter_id") or item.get("source_adapter_id"),
            "direction": item.get("direction"),
            "trade_type": item.get("trade_type"),
            "route_status": _route_status(item),
            "quality_status": item.get("quality_status") or item.get("proxy_quality_status"),
            "quote_normalization_status": item.get("quote_normalization_status"),
            "candidate_reject_reason": item.get("candidate_reject_reason"),
            "http_status": item.get("http_status"),
            "last": item.get("last"),
            "review_decision": (review or {}).get("decision"),
            "evidence_fingerprint": evidence_fingerprint,
            "fresh_evidence": fresh_evidence,
            "source_observation_at": source_observation_at,
            "operational_class": operational_class or "active",
            "admission_identity_mismatch": admission_identity_mismatch,
            "quality_failure_reason": proxy_short_review.get("quality_failure_reason") if proxy_short_review["applies"] else None,
            "quality_failure_reasons": proxy_short_review.get("quality_failure_reasons", []) if proxy_short_review["applies"] else [],
            "proxy_short_quality_review": proxy_short_review if proxy_short_review["applies"] else None,
        }
        conn.execute(
            """
            insert into market_admission_states (
                admission_key, venue, inst_id, data_source, market_surface, strategy_lineage,
                current_stage, highest_stage, health_status, blocker_code, session_status,
                attempts, eligible_scans, stalled_eligible_scans, consecutive_failures,
                first_seen_at, last_seen_at, last_advanced_at, last_observation_at,
                last_evidence_fingerprint, fresh_evidence_scans, stage_entered_at,
                current_episode_id, last_review_opportunity_id, last_paper_trade_id,
                terminal_class, details_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(admission_key) do update set
                current_stage = excluded.current_stage,
                highest_stage = excluded.highest_stage,
                health_status = excluded.health_status,
                blocker_code = excluded.blocker_code,
                session_status = excluded.session_status,
                attempts = market_admission_states.attempts + excluded.attempts,
                eligible_scans = market_admission_states.eligible_scans + excluded.eligible_scans,
                stalled_eligible_scans = excluded.stalled_eligible_scans,
                consecutive_failures = excluded.consecutive_failures,
                last_seen_at = excluded.last_seen_at,
                last_advanced_at = excluded.last_advanced_at,
                last_observation_at = excluded.last_observation_at,
                last_evidence_fingerprint = excluded.last_evidence_fingerprint,
                fresh_evidence_scans = market_admission_states.fresh_evidence_scans + excluded.fresh_evidence_scans,
                stage_entered_at = excluded.stage_entered_at,
                current_episode_id = coalesce(excluded.current_episode_id, market_admission_states.current_episode_id),
                last_review_opportunity_id = coalesce(excluded.last_review_opportunity_id, market_admission_states.last_review_opportunity_id),
                last_paper_trade_id = coalesce(excluded.last_paper_trade_id, market_admission_states.last_paper_trade_id),
                terminal_class = coalesce(excluded.terminal_class, market_admission_states.terminal_class),
                data_source = excluded.data_source,
                details_json = excluded.details_json
            """,
            (
                admission_key,
                _text(item.get("venue")).upper(),
                _text(item.get("inst_id") or item.get("instrument_id")),
                _data_source(item),
                _surface(item),
                lineage,
                current_stage,
                highest_stage,
                health_status,
                blocker,
                session_status,
                attempt_increment,
                eligible_increment,
                stalled,
                consecutive_failures,
                str(previous["first_seen_at"]) if previous else now,
                now,
                now if advanced else str(previous["last_advanced_at"]) if previous else now,
                source_observation_at,
                evidence_fingerprint,
                fresh_increment,
                stage_entered_at,
                episode_id,
                int(review_opportunity_id) if review_opportunity_id is not None else None,
                int(last_paper_trade_id) if last_paper_trade_id is not None else None,
                terminal_class,
                json.dumps(details, sort_keys=True),
            ),
        )
        # A watch-only observation and a later actionable direction now have
        # distinct v2 keys. Retain the old state for audit, but retire its
        # stalled diagnostic once an actionable sibling supersedes it.
        if _normalized_direction(item) not in {"watch_only", "unspecified"}:
            sibling_rows = conn.execute(
                """
                select admission_key,current_stage,current_episode_id,
                       last_evidence_fingerprint,details_json
                from market_admission_states
                where admission_key<>? and venue=? and inst_id=?
                  and market_surface=? and strategy_lineage=?
                  and blocker_code is not null
                """,
                (
                    admission_key,
                    _text(item.get("venue")).upper(),
                    _text(item.get("inst_id") or item.get("instrument_id")),
                    _surface(item),
                    lineage,
                ),
            ).fetchall()
            for sibling in sibling_rows:
                try:
                    sibling_details = json.loads(sibling["details_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    sibling_details = {}
                if _normalized_direction(sibling_details) not in {
                    "watch_only",
                    "unspecified",
                }:
                    continue
                sibling_details.update(
                    {
                        "operational_class": "superseded_directional_observation",
                        "superseded_by_admission_key": admission_key,
                    }
                )
                conn.execute(
                    """
                    update market_admission_states
                    set health_status='superseded_directional_observation',
                        blocker_code=null,stalled_eligible_scans=0,
                        consecutive_failures=0,last_seen_at=?,details_json=?
                    where admission_key=?
                    """,
                    (
                        now,
                        json.dumps(sibling_details, sort_keys=True),
                        sibling["admission_key"],
                    ),
                )
                conn.execute(
                    """
                    insert into market_admission_transitions(
                        admission_key,episode_id,occurred_at,from_stage,to_stage,
                        transition_kind,reason_code,evidence_fingerprint,details_json
                    ) values(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sibling["admission_key"],
                        sibling["current_episode_id"],
                        now,
                        sibling["current_stage"],
                        sibling["current_stage"],
                        "directional_observation_superseded",
                        "actionable_direction_observed",
                        sibling["last_evidence_fingerprint"],
                        json.dumps(
                            {"superseded_by_admission_key": admission_key},
                            sort_keys=True,
                        ),
                    ),
                )
        classification_changed = previous is not None and health_status != previous_health
        if not previous or current_stage != previous_stage or classification_changed:
            if not previous:
                transition_kind = "observed"
                transition_from = None
            elif STAGE_INDEX[current_stage] > STAGE_INDEX[previous_stage]:
                transition_kind = "advanced"
                transition_from = previous_stage
            elif current_stage != previous_stage:
                transition_kind = "regressed_current"
                transition_from = previous_stage
            else:
                transition_kind = "operational_class_changed"
                transition_from = previous_stage
            conn.execute(
                """
                insert into market_admission_transitions(
                    admission_key,episode_id,occurred_at,from_stage,to_stage,
                    transition_kind,reason_code,evidence_fingerprint,details_json
                ) values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    admission_key,
                    episode_id,
                    now,
                    transition_from,
                    current_stage,
                    transition_kind,
                    operational_class or blocker or (review or {}).get("decision"),
                    evidence_fingerprint,
                    json.dumps(
                        {
                            "fresh_evidence": fresh_evidence,
                            "highest_stage": highest_stage,
                            "review_decision": (review or {}).get("decision"),
                            "operational_class": operational_class or "active",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        if diagnostics_enabled and advanced and STAGE_INDEX[current_stage] > STAGE_INDEX[previous_stage]:
            _resolve_actions(conn, admission_key)
        state_for_action = {
            "admission_key": admission_key,
            "venue": _text(item.get("venue")).upper(),
            "inst_id": _text(item.get("inst_id") or item.get("instrument_id")),
            "current_stage": current_stage,
            "eligible_scans": (int(previous["eligible_scans"] or 0) if previous else 0) + eligible_increment,
            "stalled_eligible_scans": stalled,
            "blocker_code": blocker,
            "market_surface": _surface(item),
            "strategy_lineage": lineage,
        }
        if diagnostics_enabled:
            if stalled >= task_after:
                _upsert_task(conn, state_for_action)
            elif stalled >= diagnostic_after:
                _upsert_diagnostic(conn, state_for_action, 85)
        touched.append(admission_key)
    cohort_tasks = (
        _reconcile_cohort_tasks(conn, diagnostic_after, task_after)
        if diagnostics_enabled
        else {
            "enabled": False,
            "active_cohorts": 0,
            "active_diagnostic_cohorts": 0,
            "resolved_cohort_tasks": 0,
            "resolved_cohort_directives": 0,
            "legacy_instrument_tasks_superseded": 0,
            "legacy_instrument_directives_superseded": 0,
        }
    )
    conn.commit()

    if touched:
        placeholders = ",".join("?" for _ in touched)
        rows = conn.execute(
            f"select * from market_admission_states where admission_key in ({placeholders}) "
            "order by venue, inst_id, market_surface, admission_key",
            touched,
        ).fetchall()
    else:
        rows = []
    states = []
    for row in rows:
        output = dict(row)
        output["details"] = json.loads(output.pop("details_json") or "{}")
        states.append(output)
    return _write_report(
        conn,
        states,
        settings,
        extra_summary={"task_cohorts": cohort_tasks},
    )
