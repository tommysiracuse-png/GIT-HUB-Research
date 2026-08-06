"""Stable identities for autonomous repository work.

Every producer and every Codex worker must use the same identity before paid
implementation begins.  This module deliberately has no storage imports so it
can be shared by recommendation, coordination, and worker code without cycles.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


_WORK_STOPWORDS = {
    "a", "add", "an", "and", "as", "at", "candidate", "candidates", "change",
    "for", "from", "in", "into", "of", "on", "only", "paper", "spot", "spots",
    "the", "through", "to", "with", "fill", "fills", "trade", "trades",
}


def _nested(payload: dict[str, Any], key: str) -> Any:
    if key in payload and payload.get(key) not in (None, ""):
        return payload.get(key)
    code_change = payload.get("code_change")
    if isinstance(code_change, dict) and code_change.get(key) not in (None, ""):
        return code_change.get(key)
    return None


def _flatten_work_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_work_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_work_text(item) for item in value)
    return str(value or "")


def normalize_work_tokens(value: Any) -> list[str]:
    text = _flatten_work_text(value).lower()
    replacements = (
        (
            r"\b(?:cost[-_\s]*(?:swallowed|negative)|non[-_\s]*positive[-_\s]*net[-_\s]*edge|net[-_\s]*(?:edge[-_\s]*)?(?:after[-_\s]*)?costs?)\b",
            " net_edge_cost ",
        ),
        (r"\bmean[-_\s]*reversion\b", " mean_reversion "),
        (
            r"\b(?:gate|gated|guard|guardrail|shadow|stop|quarantine|exclude|block|filter|cap|tighten)(?:d|ing|s)?\b",
            " admission_policy ",
        ),
        (r"\b(?:decayed|decaying|decay)\b", " decay "),
        (r"\b(?:paper[-_\s]*)?(?:filled|fills?|candidates?)\b", " "),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    tokens = re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)*", text)
    normalized = {token for token in tokens if token not in _WORK_STOPWORDS and len(token) > 1}
    if "okx" in normalized and "basis" in normalized and "decay" in normalized:
        normalized.difference_update({"basis", "decay", "mean_reversion", "regime"})
        normalized.add("okx_basis_decay")
    if "frontier" in normalized and (
        "net_edge_cost" in normalized or {"cost", "edge"}.issubset(normalized)
    ):
        normalized.difference_update({"cost", "edge", "negative", "swallowed", "nonpositive"})
        normalized.add("frontier_net_edge_cost")
    return sorted(normalized)


def _with_revision(scope: str, payload: dict[str, Any]) -> str:
    revision = _nested(payload, "work_revision") or _nested(payload, "implementation_revision") or "1"
    return f"{scope}:revision:{str(revision).strip().lower()}"


def proposal_work_identity(
    row: dict[str, Any], radar: Any | None = None
) -> tuple[str, str]:
    """Return the canonical identity for one repository objective.

    A recommendation topic or owner task is preferred over inferred text.  A
    materially new implementation must explicitly advance ``work_revision``;
    fresh evidence alone updates the canonical task instead of buying another
    concurrent implementation.
    """

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    code_change = payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}
    proposal_id = str(row.get("proposal_id") or "")

    stored_fingerprint = str(
        row.get("work_fingerprint")
        or payload.get("_work_fingerprint")
        or code_change.get("_work_fingerprint")
        or ""
    ).strip()
    stored_scope = str(
        row.get("work_scope")
        or payload.get("_work_scope")
        or code_change.get("_work_scope")
        or ""
    ).strip()
    if stored_fingerprint and stored_scope:
        return stored_fingerprint, stored_scope

    topic_key = str(
        payload.get("_recommendation_topic_key")
        or code_change.get("_recommendation_topic_key")
        or ""
    ).strip().lower()
    if topic_key:
        scope = _with_revision(f"recommendation_topic:{topic_key}", payload)
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    strategy_owner_task_id = str(
        payload.get("strategy_owner_task_id")
        or evidence.get("strategy_owner_task_id")
        or ""
    ).strip().lower()
    if strategy_owner_task_id:
        scope = _with_revision(f"strategy_owner:{strategy_owner_task_id}", payload)
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    activation = code_change.get("activation_contract")
    if isinstance(activation, dict):
        adapter_id = str(activation.get("adapter_id") or "unknown").lower()
        surface = str(
            activation.get("market_surface") or payload.get("market_key") or "unknown"
        ).lower()
        scope = _with_revision(f"market_activation:{adapter_id}:{surface}", payload)
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    if radar is not None and proposal_id:
        try:
            owner = radar.execute(
                "select task_id from strategy_owner_tasks where code_proposal_id=? limit 1",
                (proposal_id,),
            ).fetchone()
        except Exception:  # The helper is also used with minimal test databases.
            owner = None
        if owner:
            scope = _with_revision(f"strategy_owner:{str(owner['task_id']).lower()}", payload)
            return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    adapter_spec_id = _nested(payload, "adapter_spec_id")
    if adapter_spec_id not in (None, ""):
        scope = _with_revision(f"adapter_spec:{adapter_spec_id}", payload)
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope

    market_key = str(payload.get("market_key") or payload.get("signal_key") or "").lower()
    target_tokens = normalize_work_tokens(market_key)
    target = "_".join(target_tokens[:10]) or str(row.get("category") or "general").lower()
    title_tokens = normalize_work_tokens(row.get("title") or payload.get("title"))
    if len(title_tokens) < 2:
        title_tokens = normalize_work_tokens(
            payload.get("proposed_change") or payload.get("rationale")
        )[:12]
    scope = _with_revision(
        f"semantic:{target}:{'_'.join(title_tokens[:14]) or 'unspecified'}",
        payload,
    )
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24], scope
