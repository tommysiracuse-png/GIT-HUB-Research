import datetime as dt


def _paper_only_scope_token(value):
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text or None


def _paper_only_scope_values(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_values = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return []
        for separator in ("|", ",", ";", "/"):
            text = text.replace(separator, " ")
        raw_values = text.split()
    values = []
    seen = set()
    for raw_value in raw_values:
        token = _paper_only_scope_token(raw_value)
        if token and token not in seen:
            seen.add(token)
            values.append(token)
    return values


def _paper_only_scope_lookup(record, *keys):
    if not isinstance(record, dict):
        return None
    containers = [record]
    for nested_key in ("metadata", "route_quality", "route_requirements", "strategy_lab", "lineage", "context"):
        nested = record.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}, (), set(), frozenset()):
                return value
    return None


def _paper_only_scope_comparable_tokens(field, value):
    token = _paper_only_scope_token(value)
    if not token:
        return set()
    tokens = {token}
    if field in {"venue", "instrument_family"}:
        tokens.update(part for part in token.split("_") if part)
    return tokens


def _paper_only_scope_matches(field, actual_value, allowed_values):
    comparable_actual = _paper_only_scope_comparable_tokens(field, actual_value)
    if not comparable_actual:
        return False
    for allowed_value in allowed_values:
        comparable_allowed = _paper_only_scope_comparable_tokens(field, allowed_value)
        if comparable_actual & comparable_allowed:
            return True
    return False


def _paper_only_strategy_scope_review(record, runtime_context=None):
    runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    strategy_lab_marker = _paper_only_scope_lookup(
        record,
        "lab_id",
        "strategy_lab_id",
        "strategy_lab_signal_id",
        "strategy_lab_candidate",
        "strategy_lab_signal",
    )
    is_strategy_lab = strategy_lab_marker not in (None, "", [], {}, ())
    declared_scope = {
        "venue": _paper_only_scope_values(_paper_only_scope_lookup(record, "allowed_venue")),
        "instrument_family": _paper_only_scope_values(_paper_only_scope_lookup(record, "allowed_instrument_family")),
        "direction": _paper_only_scope_values(_paper_only_scope_lookup(record, "allowed_direction")),
        "surface": _paper_only_scope_values(_paper_only_scope_lookup(record, "allowed_surface")),
        "quote_ccy": _paper_only_scope_values(_paper_only_scope_lookup(record, "allowed_quote_ccy")),
    }
    has_declared_scope = any(values for values in declared_scope.values())
    applies = is_strategy_lab or has_declared_scope
    runtime_scope = {
        "venue": _paper_only_scope_token(
            runtime_context.get("venue") or _paper_only_scope_lookup(record, "venue", "exchange", "broker")
        ),
        "instrument_family": _paper_only_scope_token(
            runtime_context.get("instrument_family")
            or _paper_only_scope_lookup(record, "instrument_family", "family", "market_family", "asset_class")
        ),
        "direction": _paper_only_scope_token(
            runtime_context.get("direction")
            or _paper_only_scope_lookup(record, "direction", "signal_direction", "position_direction", "side")
        ),
        "surface": _paper_only_scope_token(
            runtime_context.get("surface")
            or _paper_only_scope_lookup(record, "surface", "target_surface", "candidate_surface", "execution_surface")
        ),
        "quote_ccy": _paper_only_scope_token(
            runtime_context.get("quote_ccy")
            or _paper_only_scope_lookup(record, "quote_ccy", "quote_currency", "quote", "quote_asset")
        ),
    }
    required_fields = ("venue", "instrument_family", "direction", "surface")
    missing_scope_fields = [field for field in required_fields if is_strategy_lab and not declared_scope.get(field)]
    missing_runtime_fields = []
    mismatches = []
    for field, allowed_values in declared_scope.items():
        if not allowed_values:
            continue
        actual_value = runtime_scope.get(field)
        if not actual_value:
            if field in required_fields:
                missing_runtime_fields.append(field)
            continue
        if not _paper_only_scope_matches(field, actual_value, allowed_values):
            mismatches.append(
                {
                    "field": field,
                    "allowed": list(allowed_values),
                    "actual": actual_value,
                }
            )
    if not applies:
        return {
            "flag": "paper_only_strategy_lab_scope_guard_v1",
            "enabled": True,
            "applies": False,
            "eligible": True,
            "blocked": False,
            "rejected_by_scope": False,
            "reason": "guard_not_applicable",
            "declared_scope": declared_scope,
            "runtime_scope": runtime_scope,
            "missing_scope_fields": [],
            "missing_runtime_fields": [],
            "mismatches": [],
        }
    reason = "scope_matched"
    blocked = False
    if missing_scope_fields:
        reason = "missing_scope_fields"
        blocked = True
    elif missing_runtime_fields:
        reason = "missing_runtime_scope_fields"
        blocked = True
    elif mismatches:
        reason = "scope_mismatch"
        blocked = True
    return {
        "flag": "paper_only_strategy_lab_scope_guard_v1",
        "enabled": True,
        "applies": True,
        "eligible": not blocked,
        "blocked": blocked,
        "rejected_by_scope": blocked,
        "reason": reason,
        "declared_scope": declared_scope,
        "runtime_scope": runtime_scope,
        "missing_scope_fields": missing_scope_fields,
        "missing_runtime_fields": missing_runtime_fields,
        "mismatches": mismatches,
    }

_PAPER_ONLY_ROUTE_INTELLIGENCE = {
    "OKX": {
        "broker_surface": "okx",
        "api_surface": "public_market_data",
        "spot_short_support": "paper_shadow_only",
        "perp_support": "supported",
        "basis_support": "supported",
        "fee_reference": "public_taker_fee_schedule",
        "supported_route_sides": ["perp", "spot_plus_perp"],
        "blocked_route_sides": ["spot_short"],
        "spot_short_route_status": "blocked_for_paper_route",
        "spot_short_required_permissions": ["margin", "borrow_inventory"],
        "spot_short_margin_support": "unsupported",
        "spot_short_borrow_inventory_support": "unsupported",
        "spot_short_api_capability": "public_market_data_only",
        "basis_route_status": "supported",
        "basis_required_side": "spot_plus_perp",
        "basis_required_permissions": ["perp"],
        "basis_api_capability": "public_market_data",
        "spot_short_requirements": {
            "required_side": "spot_short",
            "required_permissions": ["margin", "borrow_inventory"],
            "margin_support": "unsupported",
            "borrow_inventory_support": "unsupported",
            "api_capability": "public_market_data_only",
            "route_status": "blocked_for_paper_route",
            "route_confidence": 0.0,
            "reason": "spot_short_requires_margin_and_borrow_inventory_not_confirmed_on_public_market_data",
        },
        "basis_requirements": {
            "required_side": "spot_plus_perp",
            "required_permissions": ["perp"],
            "perp_support": "supported",
            "api_capability": "public_market_data",
            "route_status": "supported",
            "route_confidence": 1.0,
            "reason": "perp_and_basis_legs_are_explicitly_supported_for_paper_ranking",
        },
    },
    "OKX_SPOT": {
        "broker_surface": "okx_spot",
        "api_surface": "public_market_data",
        "spot_short_support": "paper_shadow_only",
        "perp_support": "unsupported",
        "basis_support": "unsupported",
        "fee_reference": "public_spot_taker_fee_schedule",
        "supported_route_sides": [],
        "blocked_route_sides": ["spot_short", "spot_plus_perp", "perp"],
        "spot_short_route_status": "blocked_for_paper_route",
        "spot_short_required_permissions": ["margin", "borrow_inventory"],
        "spot_short_margin_support": "unsupported",
        "spot_short_borrow_inventory_support": "unsupported",
        "spot_short_api_capability": "public_market_data_only",
        "basis_route_status": "blocked_for_paper_route",
        "basis_required_side": "spot_plus_perp",
        "basis_required_permissions": ["perp"],
        "basis_api_capability": "public_market_data_only",
        "spot_short_requirements": {
            "required_side": "spot_short",
            "required_permissions": ["margin", "borrow_inventory"],
            "margin_support": "unsupported",
            "borrow_inventory_support": "unsupported",
            "api_capability": "public_market_data_only",
            "route_status": "blocked_for_paper_route",
            "route_confidence": 0.0,
            "reason": "spot_surface_does_not_confirm_margin_or_borrow_capability_for_short_routes",
        },
        "basis_requirements": {
            "required_side": "spot_plus_perp",
            "required_permissions": ["perp"],
            "perp_support": "unsupported",
            "api_capability": "public_market_data_only",
            "route_status": "blocked_for_paper_route",
            "route_confidence": 0.0,
            "reason": "basis_requires_a_perp_leg_that_is_not_supported_on_the_spot_only_surface",
        },
    },
    "GATE": {
        "broker_surface": "gate",
        "api_surface": "public_market_data",
        "spot_short_support": "paper_shadow_only",
        "perp_support": "unknown",
        "basis_support": "unknown",
        "fee_reference": "public_fee_schedule",
        "supported_route_sides": [],
        "blocked_route_sides": ["spot_short", "spot_plus_perp"],
        "spot_short_route_status": "blocked_for_paper_route",
        "spot_short_required_permissions": ["margin", "borrow_inventory"],
        "spot_short_margin_support": "unknown",
        "spot_short_borrow_inventory_support": "unknown",
        "spot_short_api_capability": "public_market_data_only",
        "basis_route_status": "blocked_for_paper_route",
        "basis_required_side": "spot_plus_perp",
        "basis_required_permissions": ["perp"],
        "basis_api_capability": "public_market_data_only",
        "spot_short_requirements": {
            "required_side": "spot_short",
            "required_permissions": ["margin", "borrow_inventory"],
            "margin_support": "unknown",
            "borrow_inventory_support": "unknown",
            "api_capability": "public_market_data_only",
            "route_status": "blocked_for_paper_route",
            "route_confidence": 0.25,
            "reason": "spot_short_route_requires_margin_and_borrow_inventory_without_explicit_confirmation",
        },
        "basis_requirements": {
            "required_side": "spot_plus_perp",
            "required_permissions": ["perp"],
            "perp_support": "unknown",
            "api_capability": "public_market_data_only",
            "route_status": "blocked_for_paper_route",
            "route_confidence": 0.25,
            "reason": "basis_route_capabilities_are_not_explicitly_confirmed_for_paper_execution",
        },
    },
}

_PAPER_ONLY_FAMILY_DECAY_SUPPRESSION_FLAG = "paper_only_family_decay_suppression_v1"
_PAPER_ONLY_FAMILY_DECAY_TARGET = {
    "market_key": "YAHOO_PROXY",
    "strategy_family": "global_proxy_momentum",
    "family": "YAHOO_PROXY|global_proxy_momentum",
    "latest_family_paper": {
        "long_proxy_standard": {
            "avg_pnl_bps": -16.225,
            "closed_count": 176,
            "score_adjustment": -7.861,
            "win_rate": 0.318,
        },
        "short_proxy_conditional": {
            "avg_pnl_bps": -24.614,
            "closed_count": 171,
            "score_adjustment": -10.755,
            "win_rate": 0.322,
        },
    },
}

_PAPER_ONLY_PROMOTION_CONTEXT_GUARD_FLAG = "paper_only_promotion_context_guard_v1"


def _paper_only_family_decay_guard_review(record, config=None):
    record_payload = record if isinstance(record, dict) else {}
    config_payload = config if isinstance(config, dict) else {}

    def _lookup(*keys):
        for container in (record_payload, config_payload):
            for key in keys:
                value = container.get(key)
                if value not in (None, "", [], {}, ()):
                    return value
        return None

    def _token(value):
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        return text or None

    def _bool_value(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        token = _token(value)
        if token in {"1", "true", "yes", "y", "on", "enabled", "allow", "allowed"}:
            return True
        if token in {"0", "false", "no", "n", "off", "disabled", "deny", "denied"}:
            return False
        return None

    def _float_value(value):
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    execution_mode = _token(
        _lookup(
            "execution_mode",
            "trading_mode",
            "mode",
            "destination_mode",
            "runner_mode",
        )
    )
    explicit_toggle = _bool_value(
        _lookup(
            _PAPER_ONLY_FAMILY_DECAY_SUPPRESSION_FLAG,
            "family_decay_suppression_enabled",
            "paper_family_decay_suppression_enabled",
        )
    )
    enabled = explicit_toggle is not False
    reason = "not_applicable"
    if execution_mode and execution_mode not in {"paper", "paper_only", "simulation", "sim", "review"}:
        enabled = False
        reason = "non_paper_mode"
    elif explicit_toggle is False:
        reason = "guard_disabled"

    market_key = str(_lookup("market_key", "signal_key", "source_market_key") or "").strip().upper() or None
    strategy_family = _token(
        _lookup(
            "strategy_family",
            "candidate_family",
            "signal_family",
            "family",
            "feature_family",
        )
    )
    applies = (
        market_key == _PAPER_ONLY_FAMILY_DECAY_TARGET["market_key"]
        and strategy_family == _PAPER_ONLY_FAMILY_DECAY_TARGET["strategy_family"]
    )
    blocked = bool(enabled and applies)
    if blocked:
        reason = "family_decay_suppressed"

    attempted_direction = _token(
        _lookup("direction", "side", "signal_side", "candidate_direction", "position_side")
    )
    freshness_state = _token(
        _lookup("freshness_state", "signal_freshness", "proxy_freshness_state", "freshness")
    ) or "unknown"

    return {
        "flag": _PAPER_ONLY_FAMILY_DECAY_SUPPRESSION_FLAG,
        "enabled": enabled,
        "applies": applies,
        "blocked": blocked,
        "eligible": not blocked,
        "reason": reason,
        "event": "family_decay_suppressed" if blocked else None,
        "family": _PAPER_ONLY_FAMILY_DECAY_TARGET["family"],
        "market_key": market_key,
        "strategy_family": strategy_family,
        "attempted_direction": attempted_direction,
        "raw_score": _float_value(_lookup("raw_score", "paper_score", "score", "candidate_score")),
        "freshness_state": freshness_state,
        "paper_score_multiplier": 0.0 if blocked else 1.0,
        "latest_family_paper": dict(_PAPER_ONLY_FAMILY_DECAY_TARGET["latest_family_paper"]),
    }


def _paper_only_freshness_now():
    return dt.datetime.now(dt.timezone.utc)


def _paper_only_parse_timestamp(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        try:
            if abs(numeric) >= 1_000_000_000_000:
                numeric /= 1000.0
            parsed = dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _paper_only_route_intelligence_text(value):
    text = str(value or "").strip()
    return text or None


def _paper_only_route_intelligence_tag(value):
    text = _paper_only_route_intelligence_text(value)
    if not text:
        return None
    return text.lower().replace("-", "_").replace(" ", "_")


def _paper_only_route_intelligence_first(record, keys):
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _paper_only_promotion_context_tokens(value):
    tokens = set()

    def _append(raw):
        text = _paper_only_route_intelligence_text(raw)
        if not text:
            return
        normalized = text.lower().replace("/", "|").replace(",", "|").replace("-", "_").replace(" ", "_")
        for token in normalized.split("|"):
            token = token.strip("_ ")
            if token:
                tokens.add(token)

    if isinstance(value, dict):
        for raw in value.values():
            _append(raw)
    elif isinstance(value, (list, tuple, set)):
        for raw in value:
            _append(raw)
    else:
        _append(value)
    return tokens


def _paper_only_promotion_context_guard_review(record, profile=None):
    source_payload = record if isinstance(record, dict) else {}
    target_payload = profile if isinstance(profile, dict) else {}

    def _lookup(container, *keys):
        return _paper_only_route_intelligence_first(container, keys)

    def _target_lookup(*keys):
        value = _lookup(target_payload, *keys)
        if value not in (None, "", [], {}, ()):
            return value
        return _lookup(source_payload, *keys)

    def _flag_value(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        token = _paper_only_route_intelligence_tag(value)
        if token in {"1", "true", "yes", "y", "on", "enabled", "allow", "allowed"}:
            return True
        if token in {"0", "false", "no", "n", "off", "disabled", "deny", "denied"}:
            return False
        return None

    def _tag(value):
        return _paper_only_route_intelligence_tag(value)

    def _direction_tokens(value):
        normalized = set()
        for token in _paper_only_promotion_context_tokens(value):
            if token in {"buy", "long", "bull", "up", "long_only"}:
                normalized.add("long")
            elif token in {"sell", "short", "bear", "down", "short_only"}:
                normalized.add("short")
            elif token in {"both", "two_way", "two_sided", "long_short", "bi_directional"}:
                normalized.update({"long", "short"})
            elif token in {"watch_only", "flat", "neutral", "none"}:
                normalized.add("flat")
            else:
                normalized.add(token)
        return normalized

    def _match(source_value, target_value):
        source_tokens = _direction_tokens(source_value)
        target_tokens = _direction_tokens(target_value)
        if not source_tokens or not target_tokens:
            return None
        return source_tokens.issubset(target_tokens)

    execution_mode = _tag(_target_lookup("execution_mode", "trading_mode", "mode", "destination_mode", "runner_mode"))
    explicit_toggle = _flag_value(
        _target_lookup(
            _PAPER_ONLY_PROMOTION_CONTEXT_GUARD_FLAG,
            "promotion_context_guard_enabled",
            "paper_promotion_context_guard_enabled",
        )
    )
    enabled = explicit_toggle is not False
    if execution_mode and execution_mode not in {"paper", "paper_only", "simulation", "sim", "review"}:
        enabled = False

    source_context = {
        "venue_class": _tag(_lookup(source_payload, "recommendation_venue_class", "source_venue_class", "discovery_venue_class", "venue_class")),
        "market_surface": _tag(_lookup(source_payload, "recommendation_market_surface", "discovery_market_surface", "source_market_surface", "market_surface", "surface")),
        "trade_family": _tag(_lookup(source_payload, "recommendation_trade_family", "discovery_trade_family", "source_trade_family", "trade_family", "family")),
        "direction": _direction_tokens(_lookup(source_payload, "recommendation_direction", "direction", "side", "signal_side")),
    }
    target_context = {
        "venue_class": _tag(_target_lookup("target_venue_class", "venue_class", "runtime_venue_class")),
        "market_surface": _tag(_target_lookup("target_market_surface", "market_surface", "runtime_market_surface")),
        "trade_family": _tag(_target_lookup("target_trade_family", "trade_family", "runtime_trade_family")),
        "direction": _direction_tokens(_target_lookup("target_direction", "direction", "side", "signal_side")),
    }

    checks = {
        "venue_class": bool(source_context["venue_class"]) and bool(target_context["venue_class"]) and source_context["venue_class"] == target_context["venue_class"],
        "market_surface": bool(source_context["market_surface"]) and bool(target_context["market_surface"]) and source_context["market_surface"] == target_context["market_surface"],
        "trade_family": bool(source_context["trade_family"]) and bool(target_context["trade_family"]) and source_context["trade_family"] == target_context["trade_family"],
        "direction": _match(source_context["direction"], target_context["direction"]),
    }
    if enabled and any(value is False for value in checks.values()):
        return {"allowed": False, "reason": "paper_promotion_context_mismatch", "checks": checks}
    return {"allowed": True, "reason": "paper_promotion_context_ok", "checks": checks}


_PAPER_ONLY_YAHOO_CRYPTO_FRESHNESS_POLICY = {
    "max_source_quote_age_seconds": 90.0,
    "max_destination_proxy_age_seconds": 90.0,
}

_PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY = {
    "enabled": True,
    "max_destination_spread_bps": 8.0,
    "min_destination_liquidity_score": 0.65,
    "min_native_proxy_momentum_bps": 5.0,
    "min_native_proxy_regime_windows": 3,
    "min_native_proxy_positive_window_rate": 0.67,
    "quarantined_target_surfaces": ("OKX_SPOT", "OKX_PERP"),
    "allow_native_proxy_monitoring": True,
    "min_target_surface_closed_count": 20,
    "min_target_surface_expectancy_net_bps": 0.0,
    "min_target_surface_quality_rate": 0.5,
    "max_target_surface_evidence_age_hours": 168.0,
    "reenable_condition": (
        "decisively_positive_stable_native_proxy_regime_and_independent_local_frontier_"
        "spread_liquidity_trend_confirmation"
    ),
}

def paper_only_yahoo_proxy_okx_target_review(record, profile=None):
    """Classify the destination for the bounded Yahoo-to-OKX quarantine.

    Only destination fields are inspected.  Source lineage is deliberately
    excluded so a native Yahoo observation cannot be mistaken for an OKX
    candidate merely because both contexts are present in one packet.
    """

    destination_containers = []
    for root in (record, profile):
        if not isinstance(root, dict):
            continue
        destination_containers.append(root)
        for key in (
            "candidate",
            "destination_context",
            "route_context",
            "local_confirmation",
            "intraday_features",
        ):
            nested = root.get(key)
            if isinstance(nested, dict):
                destination_containers.append(nested)

    def _values(*keys):
        return [
            str(container.get(key) or "").strip().lower().replace("-", "_")
            for container in destination_containers
            for key in keys
            if container.get(key) not in (None, "", [], {}, ())
        ]

    explicit_venues = _values("target_venue", "execution_venue", "destination_venue")
    venue_values = explicit_venues or _values("venue")
    surface_values = _values(
        "target_surface",
        "destination_surface",
        "candidate_surface",
        "execution_surface",
        "market_surface",
        "market_type",
        "asset_class",
        "product_type",
        "trade_type",
        "direction",
    )
    instrument_values = _values("inst_id", "instrument_id", "symbol")
    route_values = _values("route_id", "market_key")
    all_destination_values = venue_values + surface_values + instrument_values + route_values
    destination_tokens = {
        token
        for value in all_destination_values
        for token in re.findall(r"[a-z0-9]+", value)
    }
    destination_is_okx = "okx" in destination_tokens

    perp_tokens = {"perp", "perpetual", "swap", "derivative", "derivatives"}
    spot_evidence = "spot" in destination_tokens
    perp_evidence = bool(destination_tokens.intersection(perp_tokens))
    if destination_is_okx and not spot_evidence and not perp_evidence:
        # Public OKX instrument ids use the SWAP suffix for perpetuals; a
        # conventional base/quote id without that suffix is a spot surface.
        normalized_instruments = "|".join(instrument_values)
        if "swap" in normalized_instruments:
            perp_evidence = True
        elif instrument_values and any(
            quote in destination_tokens for quote in ("usdt", "usdc", "btc", "eth")
        ):
            spot_evidence = True
        elif "frontier_crypto_venue_map" in surface_values:
            spot_evidence = True

    if destination_is_okx and perp_evidence:
        target_surface = "OKX_PERP"
    elif destination_is_okx and spot_evidence:
        target_surface = "OKX_SPOT"
    else:
        target_surface = None
    quarantined = target_surface in _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
        "quarantined_target_surfaces"
    ]
    return {
        "destination_venue": "okx" if destination_is_okx else (venue_values[0] if venue_values else None),
        "target_surface": target_surface,
        "quarantined": quarantined,
        "quarantined_target_surfaces": list(
            _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY["quarantined_target_surfaces"]
        ),
        "allow_native_proxy_monitoring": True,
        "reenable_condition": _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "reenable_condition"
        ],
    }


def paper_only_proxy_frontier_target_evidence_review(record, profile=None, *, now=None):
    """Validate fresh, exact-surface paper proof for a proxy transfer.

    Evidence is accepted only from an explicit nested paper-proof packet.  A
    candidate's current quote, local alignment, or inherited proxy metrics are
    not target-surface observations and therefore cannot release quarantine.
    """

    record = record if isinstance(record, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    target_review = paper_only_yahoo_proxy_okx_target_review(record, profile)
    target_surface = target_review.get("target_surface")
    scope_containers = [record]
    for field in (
        "source_context",
        "lineage_context",
        "recommendation_lineage",
        "proxy_context",
        "destination_context",
        "candidate",
    ):
        nested = record.get(field)
        if isinstance(nested, dict):
            scope_containers.append(nested)
    scope_text = "|".join(
        str(container.get(field) or "").strip().lower().replace("-", "_")
        for container in scope_containers
        for field in (
            "source_family",
            "source_market_family",
            "origin_market_family",
            "data_source_family",
            "observation_source_family",
            "origin_surface",
            "source_surface",
            "signal_family",
            "feature_family",
            "strategy_family",
            "trade_type",
            "market_key",
            "lineage_tags",
        )
        if container.get(field) not in (None, "", [], {}, ())
    )
    source_is_proxy = "proxy" in scope_text or "yahoo" in scope_text
    momentum_family = "momentum" in scope_text
    execution_mode = _paper_only_route_intelligence_tag(
        _paper_only_route_intelligence_first(
            record, ("execution_mode", "trading_mode", "mode", "runner_mode")
        )
    )
    paper_mode = execution_mode in {None, "paper", "paper_only", "simulation", "sim", "review"}
    if target_surface is None:
        destination = record.get("destination_context")
        destination = destination if isinstance(destination, dict) else record
        venue = _paper_only_route_intelligence_tag(
            _paper_only_route_intelligence_first(
                destination, ("target_venue", "execution_venue", "destination_venue", "venue")
            )
        )
        raw_surface = _paper_only_route_intelligence_tag(
            _paper_only_route_intelligence_first(
                destination,
                (
                    "target_surface",
                    "destination_surface",
                    "market_surface",
                    "execution_surface",
                    "market_type",
                    "asset_class",
                ),
            )
        )
        direction = _paper_only_route_intelligence_tag(record.get("direction")) or ""
        crypto_text = "|".join(
            str(destination.get(field) or "").strip().lower()
            for field in ("asset_class", "market_surface", "trade_type", "market_family")
        )
        composite_spot = str(raw_surface or "").endswith("_spot")
        composite_perp = str(raw_surface or "").endswith(("_perp", "_swap"))
        if raw_surface in {"spot", "cash", "crypto_spot"} or composite_spot or (
            raw_surface is None and "frontier_spot" in direction
        ):
            surface_kind = "SPOT"
        elif raw_surface in {"perp", "perpetual", "swap", "crypto_derivatives"} or composite_perp or (
            raw_surface is None
            and any(token in direction for token in ("frontier_perp", "perp", "swap"))
        ):
            surface_kind = "PERP"
        else:
            surface_kind = None
        crypto_target = bool(
            surface_kind
            and venue not in {None, "yahoo", "yahoo_proxy"}
            and (
                "crypto" in crypto_text
                or "frontier" in direction
                or composite_spot
                or composite_perp
                or raw_surface in {"spot", "cash", "perp", "perpetual", "swap"}
            )
        )
        if crypto_target:
            venue_token = str(venue).upper()
            for suffix in ("_SPOT", "_PERP", "_SWAP"):
                if venue_token.endswith(suffix):
                    venue_token = venue_token[: -len(suffix)]
                    break
            target_surface = f"{venue_token}_{surface_kind}"
    applies = bool(
        paper_mode
        and source_is_proxy
        and momentum_family
        and target_surface is not None
    )

    policy = dict(_PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY)
    for container in (
        profile,
        profile.get("paper") if isinstance(profile.get("paper"), dict) else None,
        profile.get("frontier_crypto_adapter")
        if isinstance(profile.get("frontier_crypto_adapter"), dict)
        else None,
        profile.get("yahoo_proxy_cross_surface_alignment_guard")
        if isinstance(profile.get("yahoo_proxy_cross_surface_alignment_guard"), dict)
        else None,
    ):
        if not isinstance(container, dict):
            continue
        for key in (
            "min_target_surface_closed_count",
            "min_target_surface_expectancy_net_bps",
            "min_target_surface_quality_rate",
            "max_target_surface_evidence_age_hours",
        ):
            if container.get(key) is not None:
                policy[key] = container[key]

    evidence = None
    evidence_field = None
    for container in (record, profile):
        if not isinstance(container, dict):
            continue
        for field in (
            "target_surface_paper_evidence",
            "target_surface_paper_proof",
            "destination_surface_paper_evidence",
            "destination_surface_paper_stats",
        ):
            candidate = container.get(field)
            if isinstance(candidate, dict):
                evidence = candidate
                evidence_field = field
                break
        if evidence is not None:
            break

    def _number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _first(mapping, *keys):
        if not isinstance(mapping, dict):
            return None
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", [], {}, ()):
                return value
        return None

    def _paper_evidence(mapping):
        explicit = _first(mapping, "paper_only", "is_paper", "paper_evidence")
        if isinstance(explicit, bool):
            return explicit
        mode = _paper_only_route_intelligence_tag(
            _first(mapping, "execution_mode", "mode", "evidence_mode", "observation_mode")
        )
        source = _paper_only_route_intelligence_tag(_first(mapping, "evidence_source", "source"))
        return mode in {"paper", "paper_only", "simulation", "sim"} or source in {
            "persisted_paper_signal_stats",
            "paper_trade_outcomes",
            "target_surface_paper_observations",
        }

    def _surface(mapping):
        raw = _paper_only_route_intelligence_tag(
            _first(mapping, "target_surface", "market_surface", "execution_surface", "surface")
        )
        venue = _paper_only_route_intelligence_tag(
            _first(mapping, "target_venue", "execution_venue", "venue")
        )
        if raw in {"okx_spot", "okx_cash"}:
            return "OKX_SPOT"
        if raw in {"okx_perp", "okx_perpetual", "okx_swap"}:
            return "OKX_PERP"
        if venue == "okx" and raw in {"spot", "cash"}:
            return "OKX_SPOT"
        if venue == "okx" and raw in {"perp", "perpetual", "swap"}:
            return "OKX_PERP"
        if venue and raw in {"spot", "cash", "crypto_spot"}:
            return f"{venue.upper()}_SPOT"
        if venue and raw in {"perp", "perpetual", "swap", "crypto_derivatives"}:
            return f"{venue.upper()}_PERP"
        return str(raw or "").upper() or None

    def _timestamp(value):
        if isinstance(value, dt.datetime):
            parsed = value
        else:
            try:
                parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    evaluated_at = _timestamp(now) or dt.datetime.now(dt.timezone.utc)
    evidence_surface = _surface(evidence)
    closed_count = _number(
        _first(evidence, "closed_count", "closed_trades", "paper_observation_count", "sample_size")
    )
    expectancy = _number(
        _first(
            evidence,
            "expectancy_net_bps",
            "net_expectancy_bps",
            "avg_pnl_bps",
            "destination_expectancy_net_bps",
        )
    )
    quality_rate = _number(
        _first(evidence, "quality_pass_rate", "paper_quality_rate", "win_rate", "positive_rate")
    )
    observed_at = _timestamp(
        _first(evidence, "observed_at", "updated_at", "latest_observation_at", "measured_at")
    )
    raw_age_hours = (
        (evaluated_at - observed_at).total_seconds() / 3600.0
        if observed_at is not None
        else None
    )
    age_hours = max(0.0, raw_age_hours) if raw_age_hours is not None else None

    min_closed_count = max(1, int(_number(policy["min_target_surface_closed_count"]) or 20))
    min_expectancy = _number(policy["min_target_surface_expectancy_net_bps"])
    min_expectancy = 0.0 if min_expectancy is None else min_expectancy
    min_quality_rate = _number(policy["min_target_surface_quality_rate"])
    min_quality_rate = 0.5 if min_quality_rate is None else min_quality_rate
    max_age_hours = _number(policy["max_target_surface_evidence_age_hours"])
    max_age_hours = 168.0 if max_age_hours is None or max_age_hours <= 0.0 else max_age_hours

    checks = {
        "paper_observations": bool(evidence is not None and _paper_evidence(evidence)),
        "exact_target_surface": bool(target_surface and evidence_surface == target_surface),
        "minimum_closed_count": bool(closed_count is not None and closed_count >= min_closed_count),
        "positive_net_expectancy": bool(expectancy is not None and expectancy > min_expectancy),
        "quality_rate": bool(quality_rate is not None and quality_rate >= min_quality_rate),
        "fresh_observations": bool(
            raw_age_hours is not None and -(5.0 / 60.0) <= raw_age_hours <= max_age_hours
        ),
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    proof_valid = bool(target_surface and not failed_checks)
    eligible = bool(not applies or proof_valid)
    return {
        "paper_only": True,
        "applies": applies,
        "eligible": eligible,
        "promotion_eligible": eligible,
        "promotion_blocked": bool(applies and not proof_valid),
        "sandbox_rank_eligible": True,
        "maximum_stage": "paper_promotion" if eligible else "sandbox_ranking",
        "reason": (
            "not_applicable"
            if not applies
            else "fresh_target_surface_paper_evidence_validated"
            if proof_valid
            else "target_surface_paper_evidence_required"
        ),
        "failed_checks": failed_checks,
        "checks": checks,
        "evidence_field": evidence_field,
        "target_surface": target_surface,
        "evidence_surface": evidence_surface,
        "closed_count": int(closed_count) if closed_count is not None else None,
        "min_closed_count": min_closed_count,
        "expectancy_net_bps": expectancy,
        "min_expectancy_net_bps": min_expectancy,
        "quality_rate": quality_rate,
        "min_quality_rate": min_quality_rate,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "evidence_age_hours": round(age_hours, 6) if age_hours is not None else None,
        "max_evidence_age_hours": max_age_hours,
    }


def paper_only_yahoo_proxy_cross_surface_alignment_guard(record, profile=None):
    """Quarantine Yahoo momentum transferred to frontier crypto spot or perp.

    Local trend and spread measurements are retained for diagnostics, but they
    cannot make a cross-surface route eligible.  The source signal has not
    demonstrated enough native robustness to justify transfer into the faster
    crypto targets. Native Yahoo evaluation and non-paper contexts do not
    enter this policy.
    """

    record = record if isinstance(record, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    nested_keys = (
        "candidate",
        "destination_context",
        "route_context",
        "local_confirmation",
        "intraday_features",
        "native_yahoo_proxy_regime",
        "native_proxy_regime",
        "proxy_source_regime",
        "source_regime",
        "recommendation_lineage",
        "lineage_context",
        "proxy_context",
        "source_context",
    )
    containers = [record]
    for key in nested_keys:
        nested = record.get(key)
        if isinstance(nested, dict):
            containers.append(nested)

    # Generic fields such as ``venue`` must come from the candidate/destination,
    # never from a source context that happens to be traversed first.
    destination_containers = [record]
    for key in ("candidate", "destination_context", "route_context", "local_confirmation", "intraday_features"):
        nested = record.get(key)
        if isinstance(nested, dict):
            destination_containers.append(nested)

    def _lookup(*keys):
        for container in containers:
            value = _paper_only_route_intelligence_first(container, keys)
            if value not in (None, "", [], {}, ()):
                return value
        return None

    def _profile_lookup(*keys):
        config_containers = []
        for key in (
            "yahoo_proxy_cross_surface_alignment_guard",
            "frontier_crypto_adapter",
            "risk",
            "paper",
        ):
            nested = profile.get(key)
            if isinstance(nested, dict):
                config_containers.append(nested)
        config_containers.append(profile)
        for container in config_containers:
            value = _paper_only_route_intelligence_first(container, keys)
            if value not in (None, "", [], {}, ()):
                return value
        return None

    def _bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        token = _paper_only_route_intelligence_tag(value)
        if token in {"1", "true", "yes", "on", "enabled", "eligible", "passed", "aligned"}:
            return True
        if token in {"0", "false", "no", "off", "disabled", "blocked", "failed", "adverse"}:
            return False
        return None

    def _float(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    guard_config = profile.get("yahoo_proxy_cross_surface_alignment_guard")
    enabled = _bool(
        guard_config.get("enabled")
        if isinstance(guard_config, dict)
        else profile.get("yahoo_proxy_cross_surface_alignment_guard_enabled")
    )
    if enabled is None:
        enabled = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY["enabled"]

    token_fields = (
        "source_family",
        "data_source_family",
        "observation_source_family",
        "originating_signal_family",
        "origin_signal_family",
        "parent_signal_family",
        "feature_family",
        "signal_family",
        "feature_set_family",
        "market_key",
        "signal_key",
        "parent_signal_key",
        "strategy_family",
        "trade_type",
        "origin_surface",
        "source_surface",
        "target_surface",
        "candidate_surface",
        "execution_surface",
        "market_surface",
        "market_type",
        "asset_class",
        "venue",
        "execution_venue",
        "target_venue",
        "route_id",
        "lineage",
        "lineage_tags",
    )
    scope_text = "|".join(
        str(container.get(key) or "").strip().lower().replace("-", "_").replace("/", "|")
        for container in containers
        for key in token_fields
        if container.get(key) not in (None, "", [], {}, ())
    )
    # The transfer policy follows research lineage, not a single provider
    # name.  Yahoo is the common proxy source, but descendants can retain only
    # a generic ``proxy`` tag after strategy generation or serialization.
    source_is_yahoo = "yahoo_proxy" in scope_text or (
        "yahoo" in scope_text and "proxy" in scope_text
    )
    source_is_proxy = source_is_yahoo or "proxy" in scope_text
    momentum_family = "global_proxy_momentum" in scope_text or "yahoo_proxy_momentum" in scope_text or (
        "proxy" in scope_text and "momentum" in scope_text
    )

    destination_venue = _paper_only_route_intelligence_tag(
        _lookup("target_venue", "execution_venue", "destination_venue")
    )
    if destination_venue is None:
        for container in destination_containers:
            destination_venue = _paper_only_route_intelligence_tag(container.get("venue"))
            if destination_venue:
                break
    destination_is_native_proxy = destination_venue in {"yahoo", "yahoo_proxy"}
    target_review = paper_only_yahoo_proxy_okx_target_review(record, profile)
    target_surface_evidence = paper_only_proxy_frontier_target_evidence_review(record, profile)
    destination_venue = target_review.get("destination_venue") or destination_venue
    execution_mode = _paper_only_route_intelligence_tag(
        _lookup("execution_mode", "trading_mode", "mode", "destination_mode", "runner_mode")
    )
    if execution_mode is None:
        execution_mode = _paper_only_route_intelligence_tag(
            _profile_lookup("execution_mode", "trading_mode", "mode", "destination_mode", "runner_mode")
        )
    paper_mode = execution_mode in {None, "paper", "paper_only", "simulation", "sim", "review"}
    applies = bool(
        enabled
        and paper_mode
        and source_is_proxy
        and momentum_family
        and target_surface_evidence.get("applies")
        and not destination_is_native_proxy
    )

    direction_value = _paper_only_route_intelligence_tag(
        _lookup("destination_direction", "candidate_direction", "position_side", "direction", "side")
    ) or ""
    if "short" in direction_value or direction_value == "sell":
        direction = "short"
    elif "long" in direction_value or direction_value == "buy":
        direction = "long"
    else:
        direction = None

    explicit_flip = _bool(
        _lookup(
            "local_confirmation_flipped",
            "local_confirmation_flipped_negative",
            "local_trend_flipped_negative",
            "destination_confirmation_flipped",
        )
    ) is True
    explicit_confirmation = _bool(
        _lookup(
            "local_direction_confirmed",
            "destination_direction_confirmed",
            "local_market_confirms_direction",
            "destination_market_confirms_direction",
            "local_trend_confirmed",
            "local_short_horizon_confirmed",
        )
    )
    local_direction_value = _paper_only_route_intelligence_tag(
        _lookup(
            "local_short_horizon_direction",
            "local_short_horizon_trend",
            "destination_short_horizon_direction",
            "destination_short_horizon_trend",
            "local_trend_direction",
            "destination_trend_direction",
        )
    ) or ""
    if "short" in local_direction_value or local_direction_value in {"sell", "down", "negative", "bearish"}:
        local_direction = "short"
    elif "long" in local_direction_value or local_direction_value in {"buy", "up", "positive", "bullish"}:
        local_direction = "long"
    else:
        local_direction = None
    local_trend_bps = _float(
        _lookup(
            "local_short_horizon_trend_bps",
            "local_short_horizon_trend",
            "destination_short_horizon_trend_bps",
            "destination_short_horizon_trend",
            "local_return_1m_bps",
            "destination_return_1m_bps",
            "return_1m_bps",
            "local_return_5m_bps",
            "destination_return_5m_bps",
            "return_5m_bps",
            "short_horizon_return_bps",
        )
    )
    if explicit_flip:
        local_trend_state = "adverse"
    elif local_direction and direction:
        local_trend_state = "aligned" if local_direction == direction else "adverse"
    elif local_trend_bps is not None and direction:
        signed_trend = local_trend_bps if direction == "long" else -local_trend_bps
        local_trend_state = "aligned" if signed_trend > 0.0 else "adverse" if signed_trend < 0.0 else "neutral"
    elif explicit_confirmation is True:
        local_trend_state = "aligned"
    elif explicit_confirmation is False:
        local_trend_state = "not_confirmed"
    else:
        local_trend_state = "missing"
    local_direction_confirmed = local_trend_state == "aligned"

    native_momentum_bps = _float(
        _lookup(
            "native_yahoo_proxy_momentum_bps",
            "native_proxy_momentum_bps",
            "native_momentum_bps",
            "source_momentum_bps",
            "proxy_regime_momentum_bps",
            "momentum_bps",
        )
    )
    native_regime_state = _paper_only_route_intelligence_tag(
        _lookup(
            "native_yahoo_proxy_regime_state",
            "native_proxy_regime_state",
            "source_regime_state",
            "proxy_regime_state",
            "regime_state",
        )
    )
    explicit_native_stability = _bool(
        _lookup(
            "native_yahoo_proxy_regime_stable",
            "native_proxy_regime_stable",
            "source_regime_stable",
            "proxy_regime_stable",
            "regime_stable",
        )
    )
    native_regime_windows = _float(
        _lookup(
            "native_yahoo_proxy_regime_windows",
            "native_proxy_regime_windows",
            "source_regime_windows",
            "regime_window_count",
            "window_count",
        )
    )
    native_positive_window_rate = _float(
        _lookup(
            "native_yahoo_proxy_positive_window_rate",
            "native_proxy_positive_window_rate",
            "source_positive_window_rate",
            "positive_window_rate",
        )
    )
    min_native_momentum_bps = _float(_profile_lookup("min_native_proxy_momentum_bps"))
    if min_native_momentum_bps is None or min_native_momentum_bps <= 0.0:
        min_native_momentum_bps = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "min_native_proxy_momentum_bps"
        ]
    min_native_regime_windows = _float(_profile_lookup("min_native_proxy_regime_windows"))
    if min_native_regime_windows is None or min_native_regime_windows < 1.0:
        min_native_regime_windows = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "min_native_proxy_regime_windows"
        ]
    min_native_positive_window_rate = _float(
        _profile_lookup("min_native_proxy_positive_window_rate")
    )
    if min_native_positive_window_rate is None:
        min_native_positive_window_rate = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "min_native_proxy_positive_window_rate"
        ]
    min_native_positive_window_rate = max(0.0, min(1.0, min_native_positive_window_rate))

    stable_state = native_regime_state in {
        "stable_positive",
        "decisively_positive_stable",
        "positive_stable",
        "risk_on_stable",
    }
    unstable_state = native_regime_state in {
        "unstable",
        "mixed",
        "choppy",
        "indeterminate",
        "non_positive",
        "negative",
    }
    window_stability = bool(
        native_regime_windows is not None
        and native_regime_windows >= min_native_regime_windows
        and native_positive_window_rate is not None
        and native_positive_window_rate >= min_native_positive_window_rate
    )
    native_regime_stable = bool(
        not unstable_state
        and explicit_native_stability is not False
        and (
            explicit_native_stability is True
            or stable_state
            or window_stability
        )
    )
    native_regime_decisively_positive = bool(
        native_momentum_bps is not None
        and native_momentum_bps >= min_native_momentum_bps
    )

    max_spread_bps = _float(
        _lookup("max_destination_spread_bps", "maximum_spread_bps", "max_spread_bps")
    )
    if max_spread_bps is None:
        max_spread_bps = _float(
            _profile_lookup("max_destination_spread_bps", "maximum_spread_bps", "max_spread_bps")
        )
    if max_spread_bps is None or max_spread_bps <= 0.0:
        max_spread_bps = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "max_destination_spread_bps"
        ]
    explicit_spread_ok = _bool(
        _lookup(
            "local_spread_non_adverse",
            "destination_spread_non_adverse",
            "non_adverse_spread",
            "spread_condition_non_adverse",
            "destination_spread_ok",
            "local_spread_ok",
            "spread_ok",
        )
    )
    destination_spread_bps = _float(
        _lookup(
            "destination_spread_bps",
            "local_spread_bps",
            "current_spread_bps",
            "spread_bps",
            "simulated_spread_bps",
        )
    )
    if destination_spread_bps is not None:
        spread_non_adverse = 0.0 <= destination_spread_bps <= max_spread_bps
    else:
        spread_non_adverse = explicit_spread_ok is True
    spread_evidence_present = destination_spread_bps is not None or explicit_spread_ok is not None

    min_destination_liquidity_score = _float(
        _profile_lookup("min_destination_liquidity_score")
    )
    if min_destination_liquidity_score is None or min_destination_liquidity_score <= 0.0:
        min_destination_liquidity_score = _PAPER_ONLY_YAHOO_CROSS_SURFACE_ALIGNMENT_POLICY[
            "min_destination_liquidity_score"
        ]
    explicit_liquidity_ok = _bool(
        _lookup(
            "local_liquidity_ok",
            "destination_liquidity_ok",
            "liquidity_acceptable",
            "liquidity_check_passed",
        )
    )
    destination_liquidity_score = _float(
        _lookup(
            "destination_liquidity_score",
            "local_liquidity_score",
            "depth_liquidity_score",
            "liquidity_score",
        )
    )
    liquidity_evidence_present = (
        destination_liquidity_score is not None or explicit_liquidity_ok is not None
    )
    liquidity_acceptable = bool(
        explicit_liquidity_ok is True
        or (
            destination_liquidity_score is not None
            and destination_liquidity_score >= min_destination_liquidity_score
        )
    )
    local_confirmation_checks = {
        "short_horizon_trend": local_direction_confirmed,
        "spread": bool(spread_evidence_present and spread_non_adverse),
        "liquidity": bool(liquidity_evidence_present and liquidity_acceptable),
    }
    local_confirmation_passed = all(local_confirmation_checks.values())

    # The transplant is released only when the source itself is robust and a
    # separate destination observation confirms executable local conditions.
    # Exact-surface paper outcomes remain an additional portability safeguard.
    eligible = bool(
        not applies
        or (
            target_surface_evidence.get("eligible")
            and native_regime_decisively_positive
            and native_regime_stable
            and local_confirmation_passed
        )
    )
    blocked = bool(applies and not eligible)
    if not applies:
        reason = "disabled" if not enabled else "non_paper_mode" if not paper_mode else "not_applicable"
    elif not native_regime_decisively_positive:
        reason = "native_yahoo_proxy_regime_non_positive"
    elif not native_regime_stable:
        reason = "native_yahoo_proxy_regime_unstable"
    elif not local_confirmation_passed:
        reason = "local_frontier_confirmation_failed"
    elif eligible:
        reason = "fresh_target_surface_paper_evidence_validated"
    else:
        reason = "yahoo_proxy_cross_surface_quarantined"

    if direction is None:
        alignment_reason = "missing_destination_direction"
    elif local_trend_state == "missing":
        alignment_reason = "missing_local_short_horizon_trend"
    elif local_trend_state == "adverse":
        alignment_reason = "local_short_horizon_trend_adverse"
    elif not local_direction_confirmed:
        alignment_reason = "local_direction_not_confirmed"
    elif not spread_evidence_present:
        alignment_reason = "missing_destination_spread"
    elif not spread_non_adverse:
        alignment_reason = "destination_spread_adverse"
    elif not liquidity_evidence_present:
        alignment_reason = "missing_destination_liquidity"
    elif not liquidity_acceptable:
        alignment_reason = "destination_liquidity_below_floor"
    else:
        alignment_reason = "local_alignment_confirmed"

    prior_review = record.get("yahoo_proxy_cross_surface_alignment_guard")
    entry_was_confirmed = _bool(
        _lookup("entry_local_confirmation_passed", "entry_alignment_confirmed")
    ) is True or bool(isinstance(prior_review, dict) and prior_review.get("eligible"))
    force_paper_exit = bool(blocked and entry_was_confirmed)
    quarantined_target_surfaces = list(target_review.get("quarantined_target_surfaces") or ())
    reviewed_target_surface = target_surface_evidence.get("target_surface")
    if reviewed_target_surface and reviewed_target_surface not in quarantined_target_surfaces:
        quarantined_target_surfaces.append(reviewed_target_surface)
    return {
        "enabled": bool(enabled),
        "paper_only": True,
        "applies": applies,
        "eligible": eligible,
        "blocked": blocked,
        "entry_allowed": eligible,
        "emit_recommendation": eligible,
        "emit_route": eligible,
        "promotion_eligible": eligible,
        "creation_allowed": eligible,
        "paper_score_eligible": eligible,
        "paper_rank_eligible": eligible,
        # An unproven transfer may remain visible to local sandbox ranking so
        # the target surface can collect the paper observations needed for a
        # later promotion.  It must not, however, become paper-rank eligible
        # or emit a route until that exact-surface proof is fresh and meets
        # the configured quality thresholds.
        "sandbox_rank_eligible": bool(
            not applies or target_surface_evidence.get("sandbox_rank_eligible", False)
        ),
        "activation_allowed": eligible,
        "paper_allocation_multiplier": 1.0 if eligible else 0.0,
        "maximum_stage": (
            target_surface_evidence.get("maximum_stage")
            if applies
            else None
        ),
        "reason": reason,
        "alignment_reason": alignment_reason,
        "source_family": (
            "yahoo_proxy"
            if source_is_yahoo
            else "proxy_derived"
            if source_is_proxy
            else None
        ),
        "signal_family": "global_proxy_momentum" if momentum_family else None,
        "destination_venue": destination_venue,
        "target_surface": target_surface_evidence.get("target_surface") or target_review.get("target_surface"),
        "quarantined_target_surfaces": quarantined_target_surfaces,
        "allow_native_proxy_monitoring": target_review.get("allow_native_proxy_monitoring"),
        "reenable_condition": target_review.get("reenable_condition"),
        "target_surface_paper_evidence_review": target_surface_evidence,
        "destination_direction": direction,
        "local_direction": local_direction,
        "local_short_horizon_trend_bps": local_trend_bps,
        "local_trend_state": local_trend_state,
        "local_direction_confirmed": local_direction_confirmed,
        "native_proxy_momentum_bps": native_momentum_bps,
        "min_native_proxy_momentum_bps": min_native_momentum_bps,
        "native_proxy_regime_state": native_regime_state,
        "native_proxy_regime_stable": native_regime_stable,
        "native_proxy_regime_decisively_positive": native_regime_decisively_positive,
        "native_proxy_regime_windows": native_regime_windows,
        "min_native_proxy_regime_windows": int(min_native_regime_windows),
        "native_proxy_positive_window_rate": native_positive_window_rate,
        "min_native_proxy_positive_window_rate": min_native_positive_window_rate,
        "destination_spread_bps": destination_spread_bps,
        "max_destination_spread_bps": max_spread_bps,
        "spread_non_adverse": spread_non_adverse,
        "destination_liquidity_score": destination_liquidity_score,
        "min_destination_liquidity_score": min_destination_liquidity_score,
        "liquidity_acceptable": liquidity_acceptable,
        "local_confirmation_checks": local_confirmation_checks,
        "local_confirmation_passed": local_confirmation_passed,
        "entry_was_confirmed": entry_was_confirmed,
        "force_paper_exit": force_paper_exit,
        "exit_reason": "yahoo_proxy_cross_surface_quarantined" if force_paper_exit else None,
    }


def _paper_only_yahoo_proxy_crypto_freshness_review(record, profile=None, *, now=None):
    """Gate Yahoo momentum before it contributes to a paper crypto route.

    The review is deliberately fail-closed for in-scope paper routes.  It does
    not alter non-paper contexts, and it keeps the rejected contribution in
    the diagnostic packet while publishing a neutral effective contribution.
    """

    containers = [item for item in (record, profile) if isinstance(item, dict)]
    for item in tuple(containers):
        if isinstance(item.get("proxy_reuse_gate"), dict):
            containers.append(item["proxy_reuse_gate"])

    def _lookup(*keys):
        for container in containers:
            value = _paper_only_route_intelligence_first(container, keys)
            if value not in (None, "", [], {}, ()):
                return value
            for nested_key in (
                "source_context",
                "lineage_context",
                "recommendation_lineage",
                "proxy_context",
                "route_context",
            ):
                nested = container.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                value = _paper_only_route_intelligence_first(nested, keys)
                if value not in (None, "", [], {}, ()):
                    return value
        return None

    def _bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        token = _paper_only_route_intelligence_tag(value)
        if token in {"1", "true", "yes", "on", "open", "allowed", "enabled"}:
            return True
        if token in {"0", "false", "no", "off", "closed", "denied", "disabled"}:
            return False
        return None

    def _float(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    scope_tokens = set()
    for container in containers:
        for key in (
            "source_family",
            "data_source_family",
            "observation_source_family",
            "feature_family",
            "signal_family",
            "feature_set_family",
            "market_key",
            "signal_key",
            "candidate_family",
            "strategy_family",
            "trade_type",
            "origin_surface",
            "source_surface",
            "target_surface",
            "candidate_surface",
            "execution_surface",
            "market_surface",
            "market_type",
            "asset_class",
            "venue",
            "execution_venue",
            "route_id",
        ):
            raw = container.get(key)
            if raw in (None, "", [], {}, ()):
                continue
            normalized = str(raw).strip().lower().replace("-", "_").replace("/", "|")
            scope_tokens.update(token.strip("_ ") for token in normalized.split("|") if token.strip("_ "))

    joined_scope = "|".join(sorted(scope_tokens))
    source_is_yahoo = "yahoo_proxy" in joined_scope or ("yahoo" in joined_scope and "proxy" in joined_scope)
    momentum_family = "global_proxy_momentum" in joined_scope or (
        "proxy" in joined_scope and "momentum" in joined_scope
    )
    destination_is_crypto = any(
        token in joined_scope
        for token in (
            "frontier",
            "crypto",
            "spot",
            "perp",
            "perpetual",
            "swap",
            "okx",
            "bitget",
            "binance_us",
            "valr",
            "gate",
            "bybit",
            "indodax",
            "bitso",
            "mercado_bitcoin",
        )
    )

    execution_mode = _paper_only_route_intelligence_tag(
        _lookup("execution_mode", "trading_mode", "mode", "destination_mode", "runner_mode")
    )
    paper_mode = execution_mode in {None, "paper", "paper_only", "simulation", "sim", "review"}
    applies = bool(paper_mode and source_is_yahoo and momentum_family and destination_is_crypto)

    raw_contribution = _float(
        _lookup(
            "propagated_momentum_contribution",
            "momentum_contribution",
            "proxy_momentum_contribution",
            "momentum_score",
        )
    )
    if not applies:
        return {
            "enabled": True,
            "paper_only": True,
            "applies": False,
            "eligible": True,
            "blocked": False,
            "reason": "non_paper_mode" if not paper_mode else "not_applicable",
            "gate_reason": None,
            "gate_reasons": [],
            "input_momentum_contribution": raw_contribution,
            "propagated_momentum_contribution": raw_contribution,
        }

    max_source_age = _float(
        _lookup("max_source_quote_age_seconds", "source_freshness_threshold_seconds")
    )
    if max_source_age is None or max_source_age <= 0.0:
        max_source_age = _PAPER_ONLY_YAHOO_CRYPTO_FRESHNESS_POLICY["max_source_quote_age_seconds"]
    max_destination_age = _float(
        _lookup("max_destination_proxy_age_seconds", "destination_proxy_freshness_threshold_seconds")
    )
    if max_destination_age is None or max_destination_age <= 0.0:
        max_destination_age = _PAPER_ONLY_YAHOO_CRYPTO_FRESHNESS_POLICY[
            "max_destination_proxy_age_seconds"
        ]

    evaluated_at = _paper_only_parse_timestamp(
        now
        or _lookup(
            "evaluated_at",
            "evaluation_timestamp",
            "scheduler_timestamp",
            "decision_time_utc",
            "destination_timestamp",
        )
    ) or _paper_only_freshness_now()
    source_timestamp = _paper_only_parse_timestamp(
        _lookup(
            "source_quote_timestamp",
            "source_timestamp",
            "latest_bar_timestamp",
            "source_bar_end_utc",
            "last_bar_utc",
            "proxy_timestamp",
        )
    )
    source_age = _float(
        _lookup("source_quote_age_seconds", "source_age_seconds", "proxy_quote_age_seconds")
    )
    if source_age is None and source_timestamp is not None:
        source_age = max(0.0, (evaluated_at - source_timestamp).total_seconds())

    destination_age = _float(
        _lookup(
            "destination_proxy_age_seconds",
            "propagated_proxy_age_seconds",
            "destination_source_age_seconds",
        )
    )
    destination_age_basis = "explicit_destination_proxy_age"
    if destination_age is None:
        destination_age = source_age
        destination_age_basis = "source_quote_age"

    explicit_session_open = _bool(_lookup("source_session_open", "proxy_session_open"))
    session_status = _paper_only_route_intelligence_tag(
        _lookup("source_session_status", "proxy_session_status", "session_status", "market_session_status")
    )
    if explicit_session_open is not None:
        source_session_open = explicit_session_open
    elif session_status in {"open", "regular", "regular_hours", "trading", "active"}:
        source_session_open = True
    elif session_status in {"closed", "off_session", "after_hours", "halted", "holiday", "weekend"}:
        source_session_open = False
    else:
        source_session_open = None

    delayed_mode_allowed = _bool(
        _lookup(
            "allow_delayed_mode",
            "delayed_mode_allowed",
            "allow_closed_session_proxy",
            "allow_off_session_proxy",
        )
    ) is True
    delayed_mode = _paper_only_route_intelligence_tag(
        _lookup("source_handling_mode", "proxy_handling_mode", "delayed_mode", "session_handling_mode")
    )
    if delayed_mode in {"delayed", "delayed_mode", "allow_delayed", "off_session_delayed"}:
        delayed_mode_allowed = True
    session_allowed = source_session_open is True or delayed_mode_allowed

    reasons = []
    explicit_reuse_valid = _bool(_lookup("proxy_valid_for_reuse"))
    if explicit_reuse_valid is False:
        reasons.append("proxy_invalid_for_reuse")
    if source_age is None:
        reasons.append("missing_source_quote_timestamp")
    elif source_age > max_source_age:
        reasons.append("stale_source_quote")
    if not session_allowed:
        reasons.append("source_session_unknown" if source_session_open is None else "source_session_closed")
    if destination_age is None:
        reasons.append("missing_destination_proxy_age")
    elif destination_age > max_destination_age:
        reasons.append("stale_destination_proxy")

    # Freshness remains useful telemetry for every crypto destination.  The
    # hard emission quarantine covers frontier crypto spot/perp and releases
    # only on fresh, exact-surface paper proof.
    alignment_guard = paper_only_yahoo_proxy_cross_surface_alignment_guard(record, profile)
    target_review = paper_only_yahoo_proxy_okx_target_review(record, profile)
    if alignment_guard.get("blocked"):
        reasons.append("yahoo_proxy_cross_surface_quarantined")

    blocked = bool(reasons)
    return {
        "enabled": True,
        "paper_only": True,
        "applies": True,
        "eligible": not blocked,
        "blocked": blocked,
        "emit_recommendation": not blocked,
        "emit_route": not blocked,
        "reason": reasons[0] if reasons else "fresh_proxy_session_allowed",
        "gate_reason": reasons[0] if reasons else None,
        "gate_reasons": reasons,
        "source_quote_timestamp": source_timestamp.isoformat() if source_timestamp else None,
        "evaluated_at": evaluated_at.isoformat(),
        "source_quote_age_seconds": round(source_age, 3) if source_age is not None else None,
        "max_source_quote_age_seconds": max_source_age,
        "source_session_status": session_status,
        "source_session_open": source_session_open,
        "delayed_mode_allowed": delayed_mode_allowed,
        "session_allowed": session_allowed,
        "destination_proxy_age_seconds": round(destination_age, 3) if destination_age is not None else None,
        "destination_proxy_age_basis": destination_age_basis,
        "max_destination_proxy_age_seconds": max_destination_age,
        "input_momentum_contribution": raw_contribution,
        "propagated_momentum_contribution": 0.0 if blocked else raw_contribution,
        "momentum_state": "neutral" if blocked else "propagated",
        "proxy_valid_for_reuse": False if blocked else explicit_reuse_valid,
        "target_surface": target_review.get("target_surface"),
        "quarantined_target_surfaces": target_review.get("quarantined_target_surfaces"),
        "allow_native_proxy_monitoring": target_review.get("allow_native_proxy_monitoring"),
        "reenable_condition": target_review.get("reenable_condition"),
        "target_surface_paper_evidence_review": alignment_guard.get(
            "target_surface_paper_evidence_review"
        ),
    }


def _paper_only_cross_surface_seed_guard_review(record, profile=None):
    containers = []
    if isinstance(record, dict):
        containers.append(record)
    if isinstance(profile, dict):
        containers.append(profile)

    def _lookup(*keys):
        for container in containers:
            value = _paper_only_route_intelligence_first(container, keys)
            if value not in (None, "", [], {}, ()):
                return value
        return None

    def _append_tokens(tokens, value):
        text = _paper_only_route_intelligence_tag(value)
        if not text:
            return
        normalized = text.replace("/", "|").replace(",", "|")
        for token in normalized.split("|"):
            token = token.strip()
            if token:
                tokens.add(token)

    scope_tokens = set()
    for container in containers:
        for key in (
            "source_family",
            "data_source_family",
            "observation_source_family",
            "feature_family",
            "signal_family",
            "feature_set_family",
            "market_key",
            "signal_key",
            "candidate_family",
            "strategy_family",
            "directional_template",
            "variant",
            "signal_variant",
            "market_surface",
            "execution_surface",
            "market_type",
            "surface",
            "product_type",
            "target_surface",
        ):
            _append_tokens(scope_tokens, container.get(key))

    source_family = _paper_only_route_intelligence_tag(
        _lookup("source_family", "data_source_family", "observation_source_family")
    )
    feature_family = _paper_only_route_intelligence_tag(
        _lookup("feature_family", "signal_family", "feature_set_family")
    )
    if source_family is None and "yahoo_proxy" in scope_tokens:
        source_family = "yahoo_proxy"
    if feature_family is None and "global_proxy_momentum" in scope_tokens:
        feature_family = "global_proxy_momentum"

    candidate_surface = None
    for container in containers:
        candidate_surface = _paper_only_route_intelligence_surface(container, tuple(scope_tokens))
        if candidate_surface:
            break

    native_proxy_variant = bool({"long_proxy", "short_proxy", "native"} & scope_tokens)
    cross_surface_target = bool(
        {"frontier", "perp", "perpetual", "cross_surface", "non_native"} & scope_tokens
    )
    freshness_gate = _paper_only_yahoo_proxy_crypto_freshness_review(record, profile)
    alignment_guard = paper_only_yahoo_proxy_cross_surface_alignment_guard(record, profile)
    applies = bool(freshness_gate.get("applies"))
    blocked = bool(
        (applies and freshness_gate.get("blocked"))
        or (alignment_guard.get("applies") and alignment_guard.get("blocked"))
    )
    if alignment_guard.get("applies") and alignment_guard.get("blocked"):
        reason = alignment_guard.get("reason")
    else:
        reason = freshness_gate.get("reason") if applies else "not_applicable"

    return {
        "enabled": True,
        "applies": bool(applies or alignment_guard.get("applies")),
        "blocked": blocked,
        "eligible": not blocked,
        "reason": reason,
        "source_family": source_family,
        "feature_family": feature_family,
        "candidate_surface": candidate_surface,
        "native_proxy_variant": native_proxy_variant,
        "cross_surface_target": cross_surface_target,
        "emit_recommendation": not blocked,
        "emit_route": not blocked,
        "policy": (
            "proxy_frontier_cross_surface_paper_quarantine"
            if alignment_guard.get("applies")
            else "yahoo_proxy_crypto_freshness_gate"
            if applies
            else "allow"
        ),
        "gate_reason": reason if blocked else None,
        "gate_reasons": list(
            dict.fromkeys(
                [
                    *list(freshness_gate.get("gate_reasons") or ()),
                    *(
                        [alignment_guard.get("reason")]
                        if alignment_guard.get("applies") and alignment_guard.get("blocked")
                        else []
                    ),
                ]
            )
        ),
        "propagated_momentum_contribution": (
            0.0 if blocked else freshness_gate.get("propagated_momentum_contribution")
        ),
        "freshness_gate": freshness_gate,
        "alignment_guard": alignment_guard,
    }

def _paper_only_route_intelligence_surface(record, tokens):
    direct = _paper_only_route_intelligence_tag(
        _paper_only_route_intelligence_first(
            record,
            ("market_surface", "execution_surface", "market_type", "surface", "product_type"),
        )
    )
    if direct in {"spot", "cash"}:
        return "spot"
    if direct in {"perp", "perpetual", "swap", "futures"}:
        return "perp"
    if any(token in tokens for token in ("_spot", "|spot|", "short_frontier_spot", "okx_spot")):
        return "spot"
    if any(token in tokens for token in ("perp", "perpetual", "swap", "funding_capture", "perp_funding_basis")):
        return "perp"
    return None


def _paper_only_route_intelligence_direction(record, tokens):
    direct = _paper_only_route_intelligence_tag(
        _paper_only_route_intelligence_first(
            record,
            ("direction", "side", "signal_side", "candidate_direction", "position_side"),
        )
    )
    if direct in {"short", "sell"}:
        return "short"
    if direct in {"long", "buy"}:
        return "long"
    if "short" in tokens:
        return "short"
    if "long" in tokens:
        return "long"
    return None


def _paper_only_route_intelligence_venue(record, surface, tokens):
    direct = _paper_only_route_intelligence_tag(
        _paper_only_route_intelligence_first(
            record,
            ("venue", "execution_venue", "exchange", "broker", "venue_key", "market_key"),
        )
    )
    if direct:
        if direct == "okx_spot":
            return "okx"
        if direct == "mercadobitcoin":
            return "mercado_bitcoin"
        return direct
    if "okx_spot" in tokens or ("okx" in tokens and surface == "spot"):
        return "okx"
    for venue in (
        "okx",
        "bitget",
        "binance_us",
        "valr",
        "gate",
        "bybit",
        "indodax",
        "bitso",
        "mercado_bitcoin",
        "mercadobitcoin",
    ):
        if venue in tokens:
            return "mercado_bitcoin" if venue == "mercadobitcoin" else venue
    return None


def _paper_only_route_intelligence_basis_mode(record, tokens):
    direct = _paper_only_route_intelligence_tag(
        _paper_only_route_intelligence_first(
            record,
            ("basis_mode", "basis_variant", "carry_mode", "basis_subfamily"),
        )
    )
    if direct in {"funding_capture", "perp_funding_basis", "carry"}:
        return "funding_capture"
    if direct in {
        "spot_legged",
        "spot_legged_basis",
        "long_perp_short_spot",
        "short_perp_long_spot",
    }:
        return "spot_legged"
    if direct in {"convergence", "basis_convergence", "mean_reversion"}:
        return "convergence"
    if "funding_capture" in tokens or "perp_funding_basis" in tokens:
        return "funding_capture"
    if any(token in tokens for token in ("long_perp_short_spot", "short_perp_long_spot", "spot_legged", "spot_legged_basis")):
        return "spot_legged"
    return "convergence" if any(token in tokens for token in ("convergence", "basis_convergence", "mean_reversion")) else None


def _paper_only_route_intelligence_permissions(surface, *, spot_short=False, funding_capture=False, basis_trade=False):
    ordered = []
    for permission in (
        "spot_market_data" if surface == "spot" or basis_trade else None,
        "perpetuals_enabled" if surface == "perp" or funding_capture or basis_trade else None,
        "margin_enabled" if spot_short else None,
        "borrow_inventory_access" if spot_short else None,
        "collateral_transfer_capability" if funding_capture or basis_trade else None,
    ):
        if permission and permission not in ordered:
            ordered.append(permission)
    return ordered


def paper_only_route_requirement_profile(record):
    if not isinstance(record, dict):
        return {}
    text_values = []
    for key in (
        "route_id",
        "signal_key",
        "venue",
        "execution_venue",
        "exchange",
        "broker",
        "market_key",
        "market_surface",
        "execution_surface",
        "market_type",
        "surface",
        "direction",
        "side",
        "variant",
        "signal_variant",
        "strategy",
        "strategy_family",
        "directional_template",
        "candidate_family",
        "route_status",
        "status",
    ):
        value = _paper_only_route_intelligence_text(record.get(key))
        if value:
            text_values.append(value.lower())
    tokens = " | ".join(text_values)
    surface = _paper_only_route_intelligence_surface(record, tokens)
    direction = _paper_only_route_intelligence_direction(record, tokens)
    venue = _paper_only_route_intelligence_venue(record, surface, tokens)
    basis_mode = _paper_only_route_intelligence_basis_mode(record, tokens)
    conditional = "conditional" in tokens
    funding_capture = bool(
        basis_mode == "funding_capture" or any(token in tokens for token in ("funding_capture", "perp_funding_basis"))
    )
    basis_trade = bool(
        basis_mode == "spot_legged" or any(token in tokens for token in ("long_perp_short_spot", "short_perp_long_spot"))
    )
    spot_short = bool(
        surface == "spot"
        and (direction == "short" or "short_frontier_spot" in tokens or "short_spot" in tokens)
    )
    required_permissions = _paper_only_route_intelligence_permissions(
        surface,
        spot_short=spot_short,
        funding_capture=funding_capture,
        basis_trade=basis_trade,
    )
    broker_surface = ":".join(part for part in (venue, surface) if part) or None
    paper_context_key = "|".join(
        (
            venue or "unknown_venue",
            surface or "unknown_surface",
            direction or "neutral_direction",
            basis_mode or "neutral_basis_mode",
        )
    )
    if spot_short:
        route_requirement_status = "supported_with_margin_and_borrow_requirements"
    elif funding_capture or basis_trade or surface == "perp":
        route_requirement_status = "supported_with_perp_and_collateral_requirements"
    elif broker_surface:
        route_requirement_status = "paper_public_market_data_route"
    else:
        route_requirement_status = "unknown_route_profile"
    public_fee_assumptions = "public_spot_taker_fee_or_default_estimate"
    if funding_capture or basis_trade or surface == "perp":
        public_fee_assumptions = "public_perp_taker_fee_and_funding_rate_estimate"
    summary_parts = [route_requirement_status]
    if broker_surface:
        summary_parts.append(broker_surface)
    if conditional:
        summary_parts.append("conditional")
    if basis_mode:
        summary_parts.append(f"basis_mode={basis_mode}")
    if spot_short:
        summary_parts.append("spot_short_requires_margin_and_borrow")
    if funding_capture:
        summary_parts.append("funding_capture_requires_perp_access")
    if basis_trade:
        summary_parts.append("basis_trade_requires_collateral_transfer")
    return {
        "broker_surface": broker_surface,
        "paper_context_key": paper_context_key,
        "venue": venue,
        "market_surface": surface,
        "basis_mode": basis_mode,
        "direction": direction,
        "conditional": conditional,
        "api_surface": "public_market_data_only",
        "required_permissions": required_permissions,
        "spot_short_requires_margin_and_borrow": spot_short,
        "funding_capture_requires_perp_access": bool(funding_capture or surface == "perp"),
        "collateral_transfer_required": bool(funding_capture or basis_trade),
        "public_fee_assumptions": public_fee_assumptions,
        "route_requirement_status": route_requirement_status,
        "summary": "; ".join(summary_parts),
    }


def paper_only_quote_age_seconds(observation, *, now=None):
    if not isinstance(observation, dict):
        return None
    timestamp = (
        observation.get("quote_timestamp")
        or observation.get("bar_timestamp")
        or observation.get("timestamp")
        or observation.get("last_timestamp")
        or observation.get("updated_at")
    )
    parsed = _paper_only_parse_timestamp(timestamp)
    if parsed is None:
        return None
    current = now if isinstance(now, dt.datetime) else _paper_only_freshness_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (current - parsed).total_seconds())


def paper_only_data_freshness_review(observation, *, threshold_seconds=90, now=None):
    age_seconds = paper_only_quote_age_seconds(observation, now=now)
    review = {
        "stale_data": False,
        "stale_age_seconds": age_seconds,
        "freshness_threshold_seconds": float(threshold_seconds),
        "freshness_reason": None,
        "eligible": True,
    }
    if age_seconds is None:
        review["freshness_reason"] = "missing_timestamp"
        return review
    if age_seconds > float(threshold_seconds):
        review["stale_data"] = True
        review["eligible"] = False
        review["freshness_reason"] = "stale_quote_or_bar"
    return review


PAPER_ONLY_SHADOW_DIRECTION_INVERSION_FLAG = "paper_only_shadow_direction_inversion_v1"
PAPER_ONLY_SHADOW_DIRECTION_INVERSION_SCOPE = ("yahoo_proxy", "global_proxy_momentum")


def _paper_only_shadow_direction_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return None


def _paper_only_shadow_direction_text(value):
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _paper_only_shadow_direction_lookup(record, *keys):
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return None


def _paper_only_shadow_direction_enabled(config):
    if not isinstance(config, dict):
        return False
    for container in (config, config.get("feature_flags")):
        if not isinstance(container, dict):
            continue
        flag = _paper_only_shadow_direction_bool(container.get(PAPER_ONLY_SHADOW_DIRECTION_INVERSION_FLAG))
        if flag is not None:
            return flag
    return False


def paper_only_shadow_direction_inversion_review(record, *, config=None):
    review = {
        "enabled": _paper_only_shadow_direction_enabled(config),
        "applies": False,
        "scope_matched": False,
        "market_key": None,
        "family": None,
        "baseline_direction": None,
        "shadow_direction": None,
        "variant": None,
        "signal_key": None,
        "reason": "feature_disabled",
        "activation_mode": "parallel_shadow_only",
    }
    if not review["enabled"]:
        return review
    if not isinstance(record, dict):
        review["reason"] = "missing_record"
        return review

    signal_key = _paper_only_shadow_direction_text(
        _paper_only_shadow_direction_lookup(record, "signal_key", "strategy_key", "candidate_key")
    )
    signal_parts = tuple(part for part in str(signal_key or "").split("|") if part)
    market_key = _paper_only_shadow_direction_text(
        _paper_only_shadow_direction_lookup(record, "market_key", "source_market_key", "market")
    ) or (signal_parts[0] if len(signal_parts) >= 1 else None)
    family = _paper_only_shadow_direction_text(
        _paper_only_shadow_direction_lookup(
            record,
            "family",
            "strategy_family",
            "candidate_family",
            "directional_template",
            "alpha_family",
        )
    ) or (signal_parts[1] if len(signal_parts) >= 2 else None)
    baseline_direction = _paper_only_shadow_direction_text(
        _paper_only_shadow_direction_lookup(record, "direction", "side", "candidate_direction", "signal_direction")
    ) or (signal_parts[2] if len(signal_parts) >= 3 else None)
    variant = _paper_only_shadow_direction_text(
        _paper_only_shadow_direction_lookup(record, "variant", "variant_id", "entry_variant", "policy_variant")
    ) or (signal_parts[3] if len(signal_parts) >= 4 else None)

    review.update(
        {
            "market_key": market_key,
            "family": family,
            "baseline_direction": baseline_direction,
            "variant": variant,
            "signal_key": signal_key,
        }
    )
    review["scope_matched"] = (market_key, family) == PAPER_ONLY_SHADOW_DIRECTION_INVERSION_SCOPE
    if not review["scope_matched"]:
        review["reason"] = "scope_mismatch"
        return review

    if baseline_direction in {"long", "long_proxy", "buy", "bullish"}:
        review["shadow_direction"] = "short_proxy"
    elif baseline_direction in {"short", "short_proxy", "sell", "bearish"}:
        review["shadow_direction"] = "long_proxy"
    else:
        review["reason"] = "direction_unavailable"
        return review

    review["applies"] = True
    review["reason"] = "shadow_direction_inversion"
    return review
"""Targeted public order-book enrichment for frontier crypto observations."""


import collections
import concurrent.futures
import datetime as dt
import json
import math
import re
import sqlite3
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


QUALITY_NOTIONALS = (100.0, 250.0, 1000.0)
CRITICAL_ANOMALIES = {
    "crossed_book",
    "locked_book",
    "empty_book",
    "one_sided_book",
    "invalid_best_prices",
}

_DEFAULT_PUBLIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Encoding": "identity",
}

_BYBIT_READ_ONLY_BROWSER_HEADERS = {
    **_DEFAULT_PUBLIC_HEADERS,
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "DNT": "1",
    "Pragma": "no-cache",
    "Origin": "https://www.bybit.com",
    "Referer": "https://www.bybit.com/",
    'Sec-CH-UA': '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    'Sec-CH-UA-Platform': '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Priority": "u=1, i",
    "X-Requested-With": "XMLHttpRequest",
}

_BYBIT_PUBLIC_FAILOVER_HOSTS = {"api.bybit.com": "api.bytick.com", "api.bytick.com": "api2.bybit.com"}
_BYBIT_LINEAR_PERP_CANARY_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})
_BYBIT_PUBLIC_TICKER_PATH = "/v5/market/tickers"
_BYBIT_PUBLIC_ORDERBOOK_PATH = "/v5/market/orderbook"
_BYBIT_PUBLIC_ORDERBOOK_LIMIT = 1
_VALR_PUBLIC_BASE_URL = "https://api.valr.com"
_VALR_SUPPORTED_SYMBOLS = {
    "BTCZAR": {"base": "BTC", "quote": "ZAR"},
    "ETHZAR": {"base": "ETH", "quote": "ZAR"},
    "USDTZAR": {"base": "USDT", "quote": "ZAR"},
}

PAPER_ONLY_ROUTE_QUALITY_DEFAULTS = {
    "max_quote_age_ms": 15000.0,
    "warning_quote_age_fraction": 0.75,
    "max_spread_to_baseline_ratio": 2.5,
    "warning_spread_to_baseline_ratio": 1.75,
    "min_depth_to_size_ratio": 0.75,
    "warning_depth_to_size_ratio": 1.25,
}
PAPER_ONLY_ROUTE_BLOCK_STATUSES = frozenset(
    {"blocked", "down", "error", "halted", "maintenance", "offline", "unavailable"}
)
PAPER_ONLY_ROUTE_WARN_STATUSES = frozenset({"degraded", "limited", "stale"})
PAPER_ONLY_ROUTE_STATUS_ALIASES = {
    "blocked_route": "blocked",
    "route_blocked": "blocked",
    "paper_ineligible": "blocked",
    "temporarily_unavailable": "unavailable",
    "rate_limited": "limited",
    "rate-limited": "limited",
    "quote_stale": "stale",
    "stale_quote": "stale",
    "degraded_route": "degraded",
    "route_degraded": "degraded",
}
PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG = "paper_only_context_inheritance_guard_v1"
PAPER_ONLY_CONTEXT_MIN_SAMPLE_SIZE_DEFAULT = 20.0
PAPER_ONLY_CONTEXT_MIN_EXPECTANCY_BPS_DEFAULT = 0.0


def _paper_only_quality_as_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _paper_only_route_status_key(value):
    if isinstance(value, dict):
        value = value.get("route_status") or value.get("status") or value.get("route")
    return str(value or "").strip().lower()


def _paper_only_normalize_route_status_key(value):
    key = _paper_only_route_status_key(value)
    if not key:
        return key
    key = key.replace(" ", "_")
    return PAPER_ONLY_ROUTE_STATUS_ALIASES.get(key, key)


def _paper_only_context_guard_enabled(config):
    if not isinstance(config, dict):
        return False
    direct_flag = _paper_only_cross_asset_regime_bool(config.get(PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG))
    if direct_flag is not None:
        return direct_flag
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested_flag = _paper_only_cross_asset_regime_bool(feature_flags.get(PAPER_ONLY_CONTEXT_INHERITANCE_GUARD_FLAG))
        if nested_flag is not None:
            return nested_flag
    return False


def _paper_only_context_lookup(source, *keys):
    if not isinstance(source, dict):
        return None
    for key in keys:
        if key in source:
            value = source.get(key)
            if value not in (None, "", [], {}, ()):
                return value
    return None


def _paper_only_context_as_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _paper_only_context_tag(value):
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return normalized or None


def _paper_only_context_side(value):
    tag = _paper_only_context_tag(value)
    if tag in {"buy", "long"}:
        return "long"
    if tag in {"sell", "short"}:
        return "short"
    return tag


def _paper_only_context_market_type(value):
    tag = _paper_only_context_tag(value)
    if not tag:
        return None
    if "spot" in tag:
        return "spot"
    if any(token in tag for token in ("perp", "perpetual", "swap")):
        return "perp"
    if "future" in tag:
        return "futures"
    return tag


def _paper_only_context_leg_structure(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        leg_count = int(value)
        if leg_count > 0:
            return "single_leg" if leg_count == 1 else f"{leg_count}_leg"
    tag = _paper_only_context_tag(value)
    if tag in {"1", "one", "single", "single_leg", "outright"}:
        return "single_leg"
    if tag in {"2", "two", "double", "two_leg", "double_leg", "basis"}:
        return "two_leg"
    return tag


def _paper_only_context_carry_bucket(value):
    tag = _paper_only_context_tag(value)
    if tag in {"positive", "positive_carry", "carry_positive", "funding_positive"}:
        return "positive_carry"
    if tag in {"negative", "negative_carry", "carry_negative", "funding_negative"}:
        return "negative_carry"
    if tag in {"flat", "neutral", "none", "zero"}:
        return "neutral"
    return tag


def _paper_only_context_signature(record):
    if not isinstance(record, dict):
        return None
    strategy_family = _paper_only_context_tag(
        _paper_only_context_lookup(record, "strategy_family", "candidate_family", "strategy")
    )
    leg_structure = _paper_only_context_leg_structure(
        _paper_only_context_lookup(record, "leg_structure", "execution_legs", "structure", "leg_count", "legs")
    )
    if leg_structure is None and strategy_family and "basis" in strategy_family:
        leg_structure = "two_leg"
    return {
        "venue": _paper_only_context_tag(
            _paper_only_context_lookup(record, "venue", "venue_id", "exchange", "execution_venue", "route_destination")
        ),
        "market_type": _paper_only_context_market_type(
            _paper_only_context_lookup(record, "market_type", "instrument_type", "product_type", "market", "contract_type")
        ),
        "side": _paper_only_context_side(
            _paper_only_context_lookup(record, "side", "direction", "trade_side", "signal_side")
        ),
        "leg_structure": leg_structure,
        "carry_bucket": _paper_only_context_carry_bucket(
            _paper_only_context_lookup(record, "carry_bucket", "carry_regime", "funding_regime", "basis_regime", "carry")
        ),
    }


def _paper_only_context_signature_key(signature):
    if not isinstance(signature, dict):
        return None
    parts = []
    for key in ("venue", "market_type", "side", "leg_structure", "carry_bucket"):
        parts.append(f"{key}={signature.get(key) or 'unknown'}")
    return "|".join(parts)


def _paper_only_context_evidence_payload(config):
    if not isinstance(config, dict):
        return None
    direct = _paper_only_context_lookup(
        config,
        "paper_variant_context_evidence",
        "paper_context_inheritance_evidence",
        "variant_context_evidence",
    )
    if direct is not None:
        return direct
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        return _paper_only_context_lookup(
            feature_flags,
            "paper_variant_context_evidence",
            "paper_context_inheritance_evidence",
            "variant_context_evidence",
        )
    return None


def _paper_only_context_threshold(config, *keys, default=None):
    if not isinstance(config, dict):
        return default
    direct = _paper_only_context_as_float(_paper_only_context_lookup(config, *keys))
    if direct is not None:
        return direct
    feature_flags = config.get("feature_flags")
    if isinstance(feature_flags, dict):
        nested = _paper_only_context_as_float(_paper_only_context_lookup(feature_flags, *keys))
        if nested is not None:
            return nested
    return default


def _paper_only_context_evidence_entries(payload):
    if isinstance(payload, dict):
        contexts = payload.get("contexts")
        if isinstance(contexts, dict):
            for key, value in contexts.items():
                if isinstance(value, dict):
                    entry = dict(value)
                    entry.setdefault("context_signature_key", key)
                    yield entry
            return
        if isinstance(contexts, (list, tuple)):
            for entry in contexts:
                if isinstance(entry, dict):
                    yield entry
            return
        for key, value in payload.items():
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("context_signature_key", key)
                yield entry
        return
    if isinstance(payload, (list, tuple)):
        for entry in payload:
            if isinstance(entry, dict):
                yield entry


def _paper_only_context_evidence_match(signature, payload):
    if not isinstance(signature, dict):
        return None
    signature_key = _paper_only_context_signature_key(signature)
    for entry in _paper_only_context_evidence_entries(payload):
        entry_key = entry.get("context_signature_key") or entry.get("signature_key") or entry.get("key")
        if entry_key is not None and str(entry_key).strip() == str(signature_key):
            return entry
        entry_signature = entry.get("context_signature") if isinstance(entry.get("context_signature"), dict) else entry
        if _paper_only_context_signature(entry_signature) == signature:
            return entry
    return None


def _paper_only_context_evidence_review(record, config=None):
    signature = _paper_only_context_signature(record)
    review = {
        "enabled": False,
        "context_signature": signature,
        "context_signature_key": _paper_only_context_signature_key(signature) if isinstance(signature, dict) else None,
        "matched": False,
        "eligible": True,
        "sample_size": None,
        "min_sample_size": PAPER_ONLY_CONTEXT_MIN_SAMPLE_SIZE_DEFAULT,
        "expectancy_bps": None,
        "min_expectancy_bps": PAPER_ONLY_CONTEXT_MIN_EXPECTANCY_BPS_DEFAULT,
        "inherited_confidence": None,
        "variant_state": "guard_disabled",
        "activation_mode": "guard_disabled",
        "reason": "guard_disabled",
    }
    enabled = _paper_only_context_guard_enabled(config)
    if not enabled:
        return review

    review["enabled"] = True
    review["min_sample_size"] = _paper_only_context_threshold(
        config,
        "paper_context_min_sample_size",
        "paper_variant_min_sample_size",
        "minimum_paper_sample_size",
        default=PAPER_ONLY_CONTEXT_MIN_SAMPLE_SIZE_DEFAULT,
    )
    review["min_expectancy_bps"] = _paper_only_context_threshold(
        config,
        "paper_context_min_expectancy_bps",
        "paper_variant_min_expectancy_bps",
        "minimum_expectancy_bps",
        default=PAPER_ONLY_CONTEXT_MIN_EXPECTANCY_BPS_DEFAULT,
    )
    review["eligible"] = False
    review["variant_state"] = "paper_shadow_only"
    review["activation_mode"] = "paper_shadow_only"
    review["inherited_confidence"] = 0.0

    if not isinstance(signature, dict) or not any(signature.values()):
        review["reason"] = "missing_context_signature"
        return review

    payload = _paper_only_context_evidence_payload(config)
    matched_entry = _paper_only_context_evidence_match(signature, payload)
    if not isinstance(matched_entry, dict):
        review["reason"] = "missing_context_match"
        return review

    review["matched"] = True
    review["sample_size"] = _paper_only_context_as_float(
        _paper_only_context_lookup(matched_entry, "sample_size", "paper_sample_size", "trade_count", "trades")
    )
    review["expectancy_bps"] = _paper_only_context_as_float(
        _paper_only_context_lookup(matched_entry, "expectancy_bps", "paper_expectancy_bps", "edge_bps", "avg_bps")
    )

    approved_flag = _paper_only_cross_asset_regime_bool(
        _paper_only_context_lookup(
            matched_entry,
            "approved",
            "allow_inheritance",
            "recommendation_eligible",
            "paper_recommendation_eligible",
        )
    )
    if approved_flag is False:
        review["reason"] = "context_explicitly_disallowed"
        return review
    if review["sample_size"] is None or review["sample_size"] < float(review["min_sample_size"]):
        review["reason"] = "sample_below_minimum"
        return review
    if review["expectancy_bps"] is None or review["expectancy_bps"] <= float(review["min_expectancy_bps"]):
        review["reason"] = "expectancy_not_positive"
        return review

    review["eligible"] = True
    review["inherited_confidence"] = 1.0
    review["variant_state"] = "eligible"
    review["activation_mode"] = "recommendation"
    review["reason"] = "approved_context_match"
    return review

PAPER_ONLY_CROSS_ASSET_REGIME_MARKET_KEY = "paper_us_cross_asset_risk_regime"
PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS = {
    "minimum_signal_alignment": 2,
    "review_window": "next_5_trading_days",
    "mode": "paper_only",
    "execution_policy": "no_live_orders",
    "confirmation_requirement": "require at least two cross-asset signals to align before raising priority",
    "warning_key": "cross_asset_risk_off_divergence_watch",
}

def _paper_only_cross_asset_regime_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return None


def _paper_only_cross_asset_regime_watch_payload(config):
    if not isinstance(config, dict):
        return None
    direct = config.get(PAPER_ONLY_CROSS_ASSET_REGIME_MARKET_KEY)
    if isinstance(direct, dict):
        return direct
    alias = config.get("cross_asset_regime_watch")
    if isinstance(alias, dict):
        return alias
    return None


def _paper_only_cross_asset_regime_lookup(key, *sources):
    for source in sources:
        if isinstance(source, dict) and key in source:
            return source.get(key)
    return None


def _paper_only_cross_asset_regime_watch_record(config=None):
    watch_config = _paper_only_cross_asset_regime_watch_payload(config)
    signals = watch_config.get("signals") if isinstance(watch_config, dict) and isinstance(watch_config.get("signals"), dict) else {}

    enabled = False
    if isinstance(watch_config, dict):
        enabled_flag = _paper_only_cross_asset_regime_bool(watch_config.get("enabled"))
        enabled = True if enabled_flag is None else enabled_flag
    elif isinstance(config, dict):
        enabled_flag = _paper_only_cross_asset_regime_bool(config.get(PAPER_ONLY_CROSS_ASSET_REGIME_MARKET_KEY))
        if enabled_flag is None:
            feature_flags = config.get("feature_flags")
            if isinstance(feature_flags, dict):
                enabled_flag = _paper_only_cross_asset_regime_bool(feature_flags.get(PAPER_ONLY_CROSS_ASSET_REGIME_MARKET_KEY))
        enabled = bool(enabled_flag)

    minimum_alignment = _as_float(
        _paper_only_cross_asset_regime_lookup("minimum_signal_alignment", signals, watch_config, config)
    )
    if minimum_alignment is None:
        minimum_alignment = PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["minimum_signal_alignment"]
    minimum_alignment = max(2, min(3, int(round(float(minimum_alignment)))))

    equity_momentum_weakened = any(
        _paper_only_cross_asset_regime_bool(
            _paper_only_cross_asset_regime_lookup(key, signals, watch_config, config)
        )
        is True
        for key in ("equity_momentum_weakened", "broad_index_lower_highs", "intraday_breadth_weaker")
    )
    rates_or_dollar_strength_persistent = any(
        _paper_only_cross_asset_regime_bool(
            _paper_only_cross_asset_regime_lookup(key, signals, watch_config, config)
        )
        is True
        for key in ("rates_or_dollar_strength_persistent", "front_end_yields_rising", "dollar_strength_persistent")
    )
    crypto_confirms_risk_appetite = _paper_only_cross_asset_regime_bool(
        _paper_only_cross_asset_regime_lookup("crypto_confirms_risk_appetite", signals, watch_config, config)
    )
    crypto_fails_to_confirm_risk_appetite = crypto_confirms_risk_appetite is False or any(
        _paper_only_cross_asset_regime_bool(
            _paper_only_cross_asset_regime_lookup(key, signals, watch_config, config)
        )
        is True
        for key in ("crypto_fails_to_confirm_risk_appetite", "bitcoin_underperforming_equities", "ether_underperforming_equities")
    )

    aligned_signal_count = sum(
        1
        for value in (equity_momentum_weakened, rates_or_dollar_strength_persistent, crypto_fails_to_confirm_risk_appetite)
        if value
    )
    triggered = bool(enabled and equity_momentum_weakened and aligned_signal_count >= minimum_alignment)
    priority = "high" if triggered and aligned_signal_count >= 3 else ("moderate" if triggered else None)
    warning_key = PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["warning_key"]
    return {
        "market_key": PAPER_ONLY_CROSS_ASSET_REGIME_MARKET_KEY,
        "enabled": enabled,
        "mode": PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["mode"],
        "review_window": PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["review_window"],
        "execution_policy": PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["execution_policy"],
        "confirmation_requirement": PAPER_ONLY_CROSS_ASSET_REGIME_DEFAULTS["confirmation_requirement"],
        "minimum_signal_alignment": minimum_alignment,
        "aligned_signal_count": aligned_signal_count,
        "triggered": triggered,
        "priority": priority,
        "warning_key": warning_key,
        "monitoring_flags": [warning_key] if triggered else [],
        "signals": {
            "equity_momentum_weakened": equity_momentum_weakened,
            "rates_or_dollar_strength_persistent": rates_or_dollar_strength_persistent,
            "crypto_fails_to_confirm_risk_appetite": crypto_fails_to_confirm_risk_appetite,
        },
    }

def paper_only_route_quality_record(
    *,
    best_bid=None,
    best_ask=None,
    bid_size=None,
    ask_size=None,
    mid_price=None,
    spread_bps=None,
    observed_at=None,
    as_of=None,
    intended_paper_notional_usd=None,
    venue_spread_baseline_bps=None,
    route_status=None,
    config=None,
):
    """Compute a paper-only route-quality record for simulated execution review."""

    thresholds = dict(PAPER_ONLY_ROUTE_QUALITY_DEFAULTS)
    if isinstance(config, dict):
        for key in thresholds:
            if config.get(key) is not None:
                thresholds[key] = config.get(key)

    bid_price = _as_float(best_bid)
    ask_price = _as_float(best_ask)
    bid_quantity = _as_float(bid_size)
    ask_quantity = _as_float(ask_size)
    mid_value = _as_float(mid_price)
    spread_value = _as_float(spread_bps)
    if spread_value is None and bid_price is not None and ask_price is not None and bid_price > 0.0 and ask_price > 0.0:
        if mid_value is None:
            mid_value = (bid_price + ask_price) / 2.0
        if mid_value and ask_price >= bid_price:
            spread_value = ((ask_price - bid_price) / mid_value) * 10_000.0

    baseline_spread_bps = _as_float(venue_spread_baseline_bps)
    if baseline_spread_bps is not None and baseline_spread_bps <= 0.0:
        baseline_spread_bps = None
    spread_to_baseline_ratio = (
        spread_value / baseline_spread_bps
        if spread_value is not None and baseline_spread_bps is not None
        else None
    )

    intended_notional_usd = _as_float(intended_paper_notional_usd)
    if intended_notional_usd is not None:
        intended_notional_usd = abs(intended_notional_usd)
        if intended_notional_usd == 0.0:
            intended_notional_usd = None

    visible_depth_candidates = []
    if bid_price is not None and bid_quantity is not None and bid_price > 0.0 and bid_quantity > 0.0:
        visible_depth_candidates.append(bid_price * bid_quantity)
    if ask_price is not None and ask_quantity is not None and ask_price > 0.0 and ask_quantity > 0.0:
        visible_depth_candidates.append(ask_price * ask_quantity)
    visible_top_of_book_notional_usd = min(visible_depth_candidates) if visible_depth_candidates else None
    depth_to_size_ratio = (
        visible_top_of_book_notional_usd / intended_notional_usd
        if visible_top_of_book_notional_usd is not None and intended_notional_usd is not None
        else None
    )

    observed_dt = _paper_only_quality_as_datetime(observed_at)
    evaluated_dt = _paper_only_quality_as_datetime(as_of) or observed_dt
    quote_age_ms = None
    if observed_dt is not None and evaluated_dt is not None:
        quote_age_ms = max(0.0, (evaluated_dt - observed_dt).total_seconds() * 1000.0)

    route_status_key = _paper_only_normalize_route_status_key(route_status)
    hard_blockers = []
    warnings = []

    if route_status_key in PAPER_ONLY_ROUTE_BLOCK_STATUSES:
        hard_blockers.append("route_unavailable")
    elif route_status_key in PAPER_ONLY_ROUTE_WARN_STATUSES:
        warnings.append("route_status_marginal")

    if quote_age_ms is not None:
        if quote_age_ms > float(thresholds["max_quote_age_ms"]):
            hard_blockers.append("stale_quote")
        elif quote_age_ms > float(thresholds["max_quote_age_ms"]) * float(thresholds["warning_quote_age_fraction"]):
            warnings.append("quote_aging")

    if spread_to_baseline_ratio is not None:
        if spread_to_baseline_ratio > float(thresholds["max_spread_to_baseline_ratio"]):
            hard_blockers.append("spread_above_baseline")
        elif spread_to_baseline_ratio > float(thresholds["warning_spread_to_baseline_ratio"]):
            warnings.append("spread_elevated")

    if depth_to_size_ratio is not None:
        if depth_to_size_ratio < float(thresholds["min_depth_to_size_ratio"]):
            hard_blockers.append("insufficient_top_of_book_depth")
        elif depth_to_size_ratio < float(thresholds["warning_depth_to_size_ratio"]):
            warnings.append("thin_top_of_book_depth")

    status_score = 1.0
    if route_status_key in PAPER_ONLY_ROUTE_BLOCK_STATUSES:
        status_score = 0.0
    elif route_status_key in PAPER_ONLY_ROUTE_WARN_STATUSES:
        status_score = 0.5

    component_scores = [status_score]
    if quote_age_ms is not None and float(thresholds["max_quote_age_ms"]) > 0.0:
        component_scores.append(max(0.0, min(1.0, 1.0 - (quote_age_ms / float(thresholds["max_quote_age_ms"])))))
    if spread_to_baseline_ratio is not None and float(thresholds["max_spread_to_baseline_ratio"]) > 0.0:
        component_scores.append(
            max(
                0.0,
                min(
                    1.0,
                    1.0 - ((max(0.0, spread_to_baseline_ratio - 1.0)) / float(thresholds["max_spread_to_baseline_ratio"])),
                ),
            )
        )
    if depth_to_size_ratio is not None and float(thresholds["warning_depth_to_size_ratio"]) > 0.0:
        component_scores.append(
            max(0.0, min(1.0, depth_to_size_ratio / float(thresholds["warning_depth_to_size_ratio"])))
        )

    route_quality_score = round(sum(component_scores) / len(component_scores), 6) if component_scores else None
    paper_ineligible = bool(hard_blockers)
    paper_decision = "blocked" if paper_ineligible else ("degraded" if warnings else "eligible")
    if paper_ineligible:
        simulated_slippage_tier = "blocked"
        simulated_size_factor = 0.0
    elif warnings:
        simulated_slippage_tier = "elevated"
        simulated_size_factor = 0.5
    else:
        simulated_slippage_tier = "normal"
        simulated_size_factor = 1.0
    cross_asset_regime_watch = _paper_only_cross_asset_regime_watch_record(config=config)

    return {
        "paper_only": True,
        "route_status": route_status_key or None,
        "quote_age_ms": quote_age_ms,
        "effective_spread_bps": spread_value,
        "venue_spread_baseline_bps": baseline_spread_bps,
        "spread_to_baseline_ratio": spread_to_baseline_ratio,
        "visible_top_of_book_notional_usd": visible_top_of_book_notional_usd,
        "intended_paper_notional_usd": intended_notional_usd,
        "depth_to_size_ratio": depth_to_size_ratio,
        "paper_ineligible": paper_ineligible,
        "blocking_reason": hard_blockers[0] if hard_blockers else None,
        "blocking_reasons": hard_blockers,
        "warnings": warnings,
        "paper_decision": paper_decision,
        "simulated_slippage_tier": simulated_slippage_tier,
        "simulated_size_factor": simulated_size_factor,
        "route_quality_score": route_quality_score,
        "cross_asset_regime_watch": cross_asset_regime_watch,
        "monitoring_flags": list(cross_asset_regime_watch.get("monitoring_flags") or []),
        "thresholds": thresholds,
    }


def _normalize_indodax_order_book_side(levels, *, reverse=False):
    normalized = []
    if not isinstance(levels, (list, tuple)):
        return normalized
    for level in levels:
        if isinstance(level, dict):
            price = _as_float(
                level.get("price") or level.get("rate") or level.get("book_rate") or level.get("bookRate") or level.get("r")
            )
            quantity = _as_float(
                level.get("amount")
                or level.get("qty")
                or level.get("volume")
                or level.get("size")
                or level.get("a") or level.get("v"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _as_float(level[0])
            quantity = _as_float(level[1])
        else:
            continue
        if price is None or quantity is None:
            continue
        if price <= 0.0 or quantity <= 0.0:
            continue
        normalized.append((price, quantity))
    normalized.sort(key=lambda item: item[0], reverse=bool(reverse))
    return normalized


def paper_only_bybit_health_route_candidates(url):
    """Return ordered public Bybit probe candidates for paper-only health checks.

    The helper is read-only and never performs network access. It preserves the
    original URL first, then adds host failover candidates, and finally adds a
    shallow order-book quote route for the common linear canary tickers that
    can still provide a public best-bid/best-ask signal when the raw ticker
    endpoint is blocked.
    """

    text = str(url or "").strip()
    if not text:
        return []

    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        return [text]

    candidates = []
    seen = set()

    def _append(candidate_url):
        if not candidate_url or candidate_url in seen:
            return
        seen.add(candidate_url)
        candidates.append(candidate_url)

    _append(text)

    host = (parsed.netloc or "").lower()
    failover_host = _BYBIT_PUBLIC_FAILOVER_HOSTS.get(host)
    if failover_host:
        _append(urllib.parse.urlunsplit(parsed._replace(netloc=failover_host)))

    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    symbol = str(query.get("symbol") or "").strip().upper()
    category = str(query.get("category") or "").strip().lower()
    is_linear_canary = (
        host in _BYBIT_PUBLIC_FAILOVER_HOSTS or host in _BYBIT_PUBLIC_FAILOVER_HOSTS.values()
    ) and parsed.path == _BYBIT_PUBLIC_TICKER_PATH and category == "linear" and symbol in _BYBIT_LINEAR_PERP_CANARY_SYMBOLS

    if not is_linear_canary:
        return candidates

    fallback_query = urllib.parse.urlencode(
        {
            "category": "linear",
            "symbol": symbol,
            "limit": str(_BYBIT_PUBLIC_ORDERBOOK_LIMIT),
        }
    )
    fallback_hosts = [host]
    if failover_host:
        fallback_hosts.append(failover_host)
    chained_host = _BYBIT_PUBLIC_FAILOVER_HOSTS.get(failover_host or "")
    if chained_host:
        fallback_hosts.append(chained_host)
    for fallback_host in fallback_hosts:
        _append(urllib.parse.urlunsplit(parsed._replace(netloc=fallback_host, path=_BYBIT_PUBLIC_ORDERBOOK_PATH, query=fallback_query)))

    return candidates


def paper_only_public_probe_headers(url, *, extra_headers=None):
    """Return read-only headers for public venue-health probes."""

    headers = dict(_DEFAULT_PUBLIC_HEADERS)
    text = str(url or "").strip()
    host = ""
    if text:
        try:
            host = urllib.parse.urlsplit(text).netloc.lower()
        except Exception:
            host = ""

    bybit_hosts = set(_BYBIT_PUBLIC_FAILOVER_HOSTS) | set(_BYBIT_PUBLIC_FAILOVER_HOSTS.values())
    if host in bybit_hosts:
        headers.update(_BYBIT_READ_ONLY_BROWSER_HEADERS)

    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if value is None:
                continue
            headers[str(key)] = str(value)
    return headers


def _normalize_valr_symbol(symbol):
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    for separator in ("/", "-", "_", " "):
        text = text.replace(separator, "")
    return text if text in _VALR_SUPPORTED_SYMBOLS else None


def paper_only_valr_public_request_plan(symbols=None, *, trade_limit=50, base_url=_VALR_PUBLIC_BASE_URL):
    """Return a read-only VALR request plan for paper frontier scanning."""

    if symbols is None:
        requested_symbols = tuple(_VALR_SUPPORTED_SYMBOLS)
    elif isinstance(symbols, (list, tuple, set, frozenset)):
        requested_symbols = tuple(symbols)
    else:
        requested_symbols = (symbols,)

    normalized_base_url = str(base_url or _VALR_PUBLIC_BASE_URL).strip().rstrip("/") or _VALR_PUBLIC_BASE_URL
    requests = []
    health_urls = []
    seen = set()
    for symbol in requested_symbols:
        venue_symbol = _normalize_valr_symbol(symbol)
        if not venue_symbol or venue_symbol in seen:
            continue
        seen.add(venue_symbol)
        spec = _VALR_SUPPORTED_SYMBOLS[venue_symbol]
        display_symbol = f"{spec['base']}/{spec['quote']}"
        pair_base_url = f"{normalized_base_url}/v1/public/{venue_symbol}"
        endpoints = {
            "ticker": f"{pair_base_url}/marketsummary",
            "top_of_book": f"{pair_base_url}/orderbook",
            "recent_trades": f"{pair_base_url}/tradehistory?limit={max(1, int(trade_limit))}",
        }
        requests.append(
            {
                "venue": "VALR",
                "paper_only": True,
                "symbol": display_symbol,
                "venue_symbol": venue_symbol,
                "base_asset": spec["base"],
                "quote_asset": spec["quote"],
                "endpoints": endpoints,
            }
        )
        health_urls.extend([endpoints["ticker"], endpoints["top_of_book"], endpoints["recent_trades"]])

    return {
        "venue": "VALR",
        "paper_only": True,
        "request_type": "public_market_data",
        "symbols": [item["symbol"] for item in requests],
        "requests": requests,
        "health_urls": health_urls,
    }


def _paper_only_probe_status_text(report):
    if not isinstance(report, dict):
        return "unknown"

    for key in ("status", "http_status", "status_code", "code"):
        value = report.get(key)
        if value in (None, ""):
            continue
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)

    error = str(report.get("error") or report.get("reason") or "").strip()
    if error:
        return error
    return "unknown"


def _paper_only_probe_reachable(report):
    if not isinstance(report, dict):
        return False

    for key in ("reachable", "ok", "success"):
        if key in report:
            return bool(report.get(key))

    for key in ("status", "http_status", "status_code", "code"):
        value = report.get(key)
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if 200 <= numeric < 300:
            return True

    payload = report.get("payload")
    return isinstance(payload, dict) and any(
        payload.get(field) not in (None, "") for field in ("lastPrice", "bid1Price", "ask1Price")
    )


def paper_only_bybit_health_probe_trace(url, *, route_reports=None):
    """Summarize public Bybit probe attempts into stable trace fields."""

    requested_route = str(url or "").strip() or None
    candidates = paper_only_bybit_health_route_candidates(requested_route)
    reports = route_reports if isinstance(route_reports, (list, tuple)) else [route_reports] if isinstance(route_reports, dict) else []
    status_chain = []
    adapter_route_used = requested_route
    reachable = False

    for index, report in enumerate(reports):
        route = ""
        if isinstance(report, dict):
            route = str(report.get("url") or report.get("route") or "").strip()
        if not route and index < len(candidates):
            route = candidates[index]
        status = _paper_only_probe_status_text(report)
        attempt_reachable = _paper_only_probe_reachable(report)
        if route:
            adapter_route_used = route
        status_chain.append(
            {
                "route": route or None,
                "status": status,
                "reachable": attempt_reachable,
            }
        )
        if attempt_reachable:
            reachable = True
            break

    fallback_applied = any((item.get("route") or requested_route) != requested_route for item in status_chain)
    reachable_via_fallback = bool(
        reachable and status_chain and (status_chain[-1].get("route") or requested_route) != requested_route
    )

    downgrade_reason = None
    if reachable_via_fallback and status_chain and status_chain[0].get("status") == "403":
        downgrade_reason = "primary_access_denied_fallback_used"
    elif status_chain and status_chain[0].get("status") == "403":
        downgrade_reason = "primary_access_denied"
    elif fallback_applied and not reachable:
        downgrade_reason = "fallback_exhausted"

    return {
        "requested_route": requested_route,
        "adapter_route_used": adapter_route_used,
        "fallback_applied": fallback_applied,
        "status_chain": status_chain,
        "reachable_via_fallback": reachable_via_fallback,
        "downgrade_reason": downgrade_reason,
        "candidate_routes": candidates,
    }


def paper_only_enrich_venue_health_row(row, *, probe_url=None, route_reports=None):
    """Attach deterministic Bybit fallback trace fields to a health row."""

    current = dict(row) if isinstance(row, dict) else {}
    requested_route = str(
        probe_url or current.get("requested_route") or current.get("url") or current.get("request_url") or ""
    ).strip()
    venue = str(current.get("venue") or "").strip().lower()
    host = urllib.parse.urlsplit(requested_route).netloc.lower() if requested_route else ""
    bybit_hosts = set(_BYBIT_PUBLIC_FAILOVER_HOSTS) | set(_BYBIT_PUBLIC_FAILOVER_HOSTS.values())
    is_bybit = venue == "bybit" or host in bybit_hosts
    if not is_bybit:
        return current

    trace = paper_only_bybit_health_probe_trace(requested_route, route_reports=route_reports)
    current.update(trace)

    fallback_reachable = bool(trace["reachable_via_fallback"] or any(item.get("reachable") for item in trace["status_chain"]))
    if fallback_reachable:
        current["reachable"] = True
        if "is_reachable" in current:
            current["is_reachable"] = True

    return current


def _public_order_book_payload(payload=None):
    current = payload or {}
    seen = set()
    while isinstance(current, dict):
        if any(key in current for key in ("bids", "asks", "buy", "sell")):
            return current
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        for key in ("payload", "book", "order_book", "data", "result"):
            nested = current.get(key)
            if isinstance(nested, dict):
                current = nested
                break
        else:
            break
    return current if isinstance(current, dict) else {}


def _normalized_public_order_book_snapshot(bids, asks, *, venue_name):
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if best_bid is None and best_ask is None:
        book_state = "empty_book"
    elif best_bid is None or best_ask is None:
        book_state = "one_sided_book"
    elif best_bid >= best_ask:
        book_state = "crossed_book" if best_bid > best_ask else "locked_book"
    else:
        book_state = "ok"
    mid_price = None
    spread_bps = None
    if best_bid is not None and best_ask is not None and best_ask > 0.0 and best_bid > 0.0:
        mid_price = (best_bid + best_ask) / 2.0
        if mid_price > 0.0:
            spread_bps = ((best_ask - best_bid) / mid_price) * 10000.0
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "book_state": book_state,
        "level_count": len(bids) + len(asks),
        "paper_only": True,
        "read_only": True,
        "venue_name": venue_name,
    }


def normalize_indodax_order_book(payload=None):
    """
    Normalize a public INDODAX order-book payload into best bid/ask metadata.

    Supports either buy/sell or bids/asks field conventions. Read-only only.
    """
    payload = _public_order_book_payload(payload)
    bids = _normalize_indodax_order_book_side(payload.get("buy") or payload.get("bids"), reverse=True)
    asks = _normalize_indodax_order_book_side(payload.get("sell") or payload.get("asks"), reverse=False)
    return _normalized_public_order_book_snapshot(bids, asks, venue_name="INDODAX")


def normalize_bitso_order_book(payload=None):
    """
    Normalize a public BITSO order-book payload into best bid/ask metadata.

    Supports nested payload/result wrappers and common public level aliases.
    """
    payload = _public_order_book_payload(payload)
    bids = _normalize_indodax_order_book_side(payload.get("bids") or payload.get("buy"), reverse=True)
    asks = _normalize_indodax_order_book_side(payload.get("asks") or payload.get("sell"), reverse=False)
    return _normalized_public_order_book_snapshot(bids, asks, venue_name="BITSO")


def normalize_buda_order_book(payload=None):
    """
    Normalize a public BUDA order-book payload into best bid/ask metadata.

    Supports nested order_book/data wrappers and common public level aliases.
    """
    payload = _public_order_book_payload(payload)
    bids = _normalize_indodax_order_book_side(payload.get("bids") or payload.get("buy"), reverse=True)
    asks = _normalize_indodax_order_book_side(payload.get("asks") or payload.get("sell"), reverse=False)
    return _normalized_public_order_book_snapshot(bids, asks, venue_name="BUDA")


def paper_only_timestamp_alignment_diagnostic(
    payload=None,
    *,
    spot_timestamp_key="spot_timestamp",
    perp_timestamp_key="perp_timestamp",
    max_skew_seconds=2.0,
):
    """Paper-only diagnostic for spot/perp timestamp alignment quality."""

    result = {
        "eligible": False,
        "reason": "missing_source_timestamps",
        "spot_timestamp": None,
        "perp_timestamp": None,
        "skew_seconds": None,
        "max_skew_seconds": float(max_skew_seconds),
        "alignment_status": "unknown",
        "paper_only": True,
        "read_only": True,
    }
    if not isinstance(payload, dict):
        result["reason"] = "invalid_payload"
        return result

    try:
        skew_seconds = float(payload.get("spot_perp_skew_seconds"))
    except (TypeError, ValueError):
        skew_seconds = None

    def _as_datetime(value):
        normalized = _timestamp_to_iso(value)
        if not normalized:
            return None
        return _parse_iso(normalized)

    if skew_seconds is None:
        spot_value = payload.get(spot_timestamp_key, payload.get("spot_quote_timestamp", payload.get("spot_data_timestamp")))
        perp_value = payload.get(perp_timestamp_key, payload.get("perp_quote_timestamp", payload.get("perp_data_timestamp")))
        spot_dt = _as_datetime(spot_value)
        perp_dt = _as_datetime(perp_value)
        if spot_dt is None or perp_dt is None:
            result["reason"] = "invalid_source_timestamps" if spot_value is not None or perp_value is not None else result["reason"]
            return result
        result["spot_timestamp"] = spot_dt.isoformat()
        result["perp_timestamp"] = perp_dt.isoformat()
        skew_seconds = abs((spot_dt - perp_dt).total_seconds())

    result["skew_seconds"] = skew_seconds
    result["eligible"] = skew_seconds <= float(max_skew_seconds)
    result["reason"] = "eligible" if result["eligible"] else "skew_above_threshold"
    result["alignment_status"] = "aligned" if result["eligible"] else "misaligned"
    return result


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _timestamp_to_iso(value: object, unit: str = "auto") -> str | None:
    if isinstance(value, str):
        parsed = _parse_iso(value)
        if parsed is not None:
            return parsed.isoformat()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    if unit == "nanoseconds":
        numeric /= 1_000_000_000.0
    elif unit == "microseconds":
        numeric /= 1_000_000.0
    elif unit == "milliseconds":
        numeric /= 1000.0
    elif unit == "auto":
        abs_numeric = abs(numeric)
        if abs_numeric > 10_000_000_000_000_000:
            numeric /= 1_000_000_000.0
        elif abs_numeric > 10_000_000_000_000:
            numeric /= 1_000_000.0
        elif abs_numeric > 10_000_000_000:
            numeric /= 1000.0
    try:
        return dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_access_metadata(response_status: object = None, exc: Exception | None = None) -> dict:
    if exc is None and isinstance(response_status, dict):
        endpoint_access = str(response_status.get("endpoint_access") or "reachable")
        blocked_http_status = response_status.get("blocked_http_status")
        blocked_reason = response_status.get("blocked_reason")
        if _bybit_access_missing_last_price(response_status):
            endpoint_access = "unavailable"
            blocked_reason = blocked_reason or "missing_last_price"
        return {
            "endpoint_access": endpoint_access,
            "blocked_http_status": blocked_http_status,
            "blocked_reason": blocked_reason,
        }
    if exc is None:
        return {
            "endpoint_access": "reachable",
            "blocked_http_status": None,
            "blocked_reason": None,
        }

    blocked_http_status = None
    blocked_reason = None
    endpoint_access = "unavailable"
    if isinstance(exc, urllib.error.HTTPError):
        blocked_http_status = str(exc.code)
        if exc.code in {401, 403, 451}:
            endpoint_access = "restricted"
            blocked_reason = f"http_{exc.code}"
    return {
        "endpoint_access": endpoint_access,
        "blocked_http_status": blocked_http_status,
        "blocked_reason": blocked_reason,
    }


def _is_bybit_linear_perp_canary_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(str(url or ""))
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"api.bybit.com", "api.bytick.com"}:
        return False
    query = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=False)
    category = str((query.get("category") or [None])[0] or "").strip().lower()
    symbol = str((query.get("symbol") or [None])[0] or "").strip().upper()
    return category == "linear" and symbol in _BYBIT_LINEAR_PERP_CANARY_SYMBOLS


def _bybit_ticker_last_price(payload: object) -> float | None:
    if isinstance(payload, dict):
        for key in ("last_price", "lastPrice", "ticker_last_price", "markPrice", "indexPrice", "price"):
            value = _as_float(payload.get(key))
            if value is not None and math.isfinite(value) and value > 0.0:
                return value
        for key in ("payload", "response", "result", "data", "ticker"):
            if key in payload:
                value = _bybit_ticker_last_price(payload.get(key))
                if value is not None:
                    return value
        list_payload = payload.get("list")
        if isinstance(list_payload, (list, tuple)):
            value = _bybit_ticker_last_price(list_payload)
            if value is not None:
                return value
        return None

    if isinstance(payload, (list, tuple)):
        for item in payload:
            value = _bybit_ticker_last_price(item)
            if value is not None:
                return value
        return None

    value = _as_float(payload)
    if value is None or not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _bybit_access_missing_last_price(access: dict | None) -> bool:
    if not isinstance(access, dict):
        return False
    endpoint_access = str(access.get("endpoint_access") or "reachable").strip().lower()
    if endpoint_access != "reachable":
        return False
    candidate_urls = (
        access.get("url"),
        access.get("requested_url"),
        access.get("effective_url"),
        access.get("fallback_url"),
    )
    if not any(_is_bybit_linear_perp_canary_url(url) for url in candidate_urls if url):
        return False
    if _bybit_ticker_last_price(access) is not None:
        return False
    return True


def _route_health_state(
    primary_access: dict | None = None,
    fallback_access: dict | None = None,
    fallback_attempted: bool = False,
) -> str:
    primary_state = (primary_access or {}).get("endpoint_access") or "unknown"
    fallback_state = (fallback_access or {}).get("endpoint_access") or "unknown"
    if _bybit_access_missing_last_price(primary_access):
        primary_state = "unavailable"
    if _bybit_access_missing_last_price(fallback_access):
        fallback_state = "unavailable"

    if primary_state == "reachable":
        return "reachable"
    if fallback_attempted and fallback_state == "reachable":
        return "reachable_via_fallback"
    if primary_state == "restricted" and not fallback_attempted:
        return "restricted"
    if primary_state == "restricted" and fallback_attempted:
        return "restricted_with_failed_fallback"
    return primary_state


def _route_access_report(
    requested_url: str,
    effective_url: str | None,
    primary_access: dict | None,
    fallback_url: str | None = None,
    fallback_access: dict | None = None,
    fallback_attempted: bool = False,
) -> dict:
    primary = {
        "url": requested_url,
        **(primary_access or _response_access_metadata()),
    }
    fallback = None
    if fallback_url:
        fallback = {
            "url": fallback_url,
            **(
                fallback_access
                or {
                    "endpoint_access": "not_attempted",
                    "blocked_http_status": None,
                    "blocked_reason": None,
                }
            ),
        }
    primary_state = primary.get("endpoint_access") or "unknown"
    fallback_state = (fallback or {}).get("endpoint_access") or "unknown"
    if fallback_attempted and fallback_state == "reachable":
        resolution = "fallback"
    elif primary_state == "reachable":
        resolution = "primary"
    elif fallback_url:
        resolution = f"{primary_state}_then_{fallback_state}"
    else:
        resolution = primary_state
    is_canary = any(
        _is_bybit_linear_perp_canary_url(candidate)
        for candidate in (requested_url, effective_url, fallback_url)
        if candidate
    )
    return {
        "requested_url": requested_url,
        "effective_url": effective_url or requested_url,
        "fallback_candidate_url": fallback_url,
        "fallback_attempted": bool(fallback_attempted),
        "resolution": resolution,
        "route_health": {
            "state": _route_health_state(
                primary_access=primary_access,
                fallback_access=fallback_access,
                fallback_attempted=fallback_attempted,
            ),
            "bybit_linear_perp_canary": is_canary,
            "paper_only": True,
            "read_only": True,
        },
        "primary": primary,
        "fallback": fallback,
    }


def _build_public_request(url: str, headers: dict | None = None) -> urllib.request.Request:
    request_headers = dict(_DEFAULT_PUBLIC_HEADERS)
    if _is_bybit_linear_perp_canary_url(url):
        request_headers.update(_BYBIT_READ_ONLY_BROWSER_HEADERS)
    if headers:
        request_headers.update(headers)
    return urllib.request.Request(url, headers=request_headers)


def _bybit_public_failover_url(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "api.bybit.com":
        return None
    if not str(parsed.path or "").startswith("/v5/market/"):
        return None
    replacement_host = _BYBIT_PUBLIC_FAILOVER_HOSTS.get(hostname)
    if not replacement_host:
        return None
    netloc = replacement_host
    if parsed.port is not None:
        netloc = f"{replacement_host}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme or "https", netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _open_json_request(request: urllib.request.Request, timeout: int, started: float) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
        received_at = _utc_now()
        return {
            "ok": True,
            "status": "reachable",
            "http_status": str(response.status),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "received_at": received_at,
            "payload": payload,
            **_response_access_metadata(response.status),
        }


def _fetch_json(url: str, timeout: int) -> dict:
    started = time.perf_counter()
    received_at = _utc_now()
    try:
        result = _open_json_request(_build_public_request(url), timeout, started)
        primary_access = {
            "endpoint_access": result.get("endpoint_access"),
            "blocked_http_status": result.get("blocked_http_status"),
            "blocked_reason": result.get("blocked_reason"),
        }
        result.update(
            {
                "requested_url": url,
                "source_url": url,
                "effective_url": url,
                "fallback_url": None,
                "fallback_used": False,
                "fallback_status": None,
                "primary_http_status": None,
                "route_access": _route_access_report(
                    requested_url=url,
                    effective_url=url,
                    primary_access=primary_access,
                    fallback_url=None,
                    fallback_access=None,
                    fallback_attempted=False,
                ),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        fallback_url = None
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
            fallback_url = _bybit_public_failover_url(url)
        primary_access = _response_access_metadata(exc=exc)
        if fallback_url:
            try:
                result = _open_json_request(
                    _build_public_request(fallback_url, _BYBIT_READ_ONLY_BROWSER_HEADERS),
                    timeout,
                    started,
                )
                fallback_access = {
                    "endpoint_access": result.get("endpoint_access"),
                    "blocked_http_status": result.get("blocked_http_status"),
                    "blocked_reason": result.get("blocked_reason"),
                }
                result.update(
                    {
                        "requested_url": url,
                        "source_url": fallback_url,
                        "effective_url": fallback_url,
                        "fallback_url": fallback_url,
                        "fallback_used": True,
                        "fallback_status": "fallback_success",
                        "primary_http_status": str(exc.code),
                        "route_access": _route_access_report(
                            requested_url=url,
                            effective_url=fallback_url,
                            primary_access=primary_access,
                            fallback_url=fallback_url,
                            fallback_access=fallback_access,
                            fallback_attempted=True,
                        ),
                    }
                )
                return result
            except Exception as fallback_exc:  # noqa: BLE001
                status = (
                    "blocked"
                    if isinstance(fallback_exc, urllib.error.HTTPError) and fallback_exc.code in {401, 403, 451}
                    else "unavailable"
                )
                fallback_access = _response_access_metadata(exc=fallback_exc)
                received_at = _utc_now()
                return {
                    "ok": False,
                    "status": status,
                    "http_status": str(fallback_exc)[:300],
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "received_at": received_at,
                    "payload": None,
                    "requested_url": url,
                    "source_url": fallback_url,
                    "effective_url": fallback_url,
                    "fallback_url": fallback_url,
                    "fallback_used": True,
                    "fallback_status": "blocked_403" if getattr(fallback_exc, "code", None) == 403 else "fallback_failed",
                    "primary_http_status": str(exc.code),
                    "route_access": _route_access_report(
                        requested_url=url,
                        effective_url=fallback_url,
                        primary_access=primary_access,
                        fallback_url=fallback_url,
                        fallback_access=fallback_access,
                        fallback_attempted=True,
                    ),
                    **_response_access_metadata(exc=fallback_exc),
                }
            received_at = _utc_now()
        status = "blocked" if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403, 451} else "unavailable"
        return {
            "ok": False,
            "status": status,
            "http_status": str(exc)[:300],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "received_at": received_at,
            "requested_url": url,
            "source_url": url,
            "effective_url": url,
            "fallback_url": None,
            "fallback_used": False,
            "fallback_status": "blocked_403" if getattr(exc, "code", None) == 403 and _bybit_public_failover_url(url) else None,
            "primary_http_status": str(exc.code) if isinstance(exc, urllib.error.HTTPError) else None,
            "route_access": _route_access_report(
                requested_url=url,
                effective_url=url,
                primary_access=primary_access,
                fallback_url=fallback_url,
                fallback_access=None,
                fallback_attempted=False,
            ),
            "payload": None,
            **_response_access_metadata(exc=exc),
        }


def _normalize_bitso_symbol(symbol: str) -> str:
    value = re.sub(r"\s+", "", str(symbol or "")).strip().lower()
    if not value:
        return value
    value = value.replace("-", "_").replace("/", "_").replace(":", "_")
    if "_" in value:
        parts = [part for part in value.split("_") if part]
        while parts and parts[-1] in {"spot", "book", "orderbook"}:
            parts = parts[:-1]
        while len(parts) >= 3 and parts[-1] == parts[-2]:
            parts = parts[:-1]
        return "_".join(parts)
    if len(value) > 3 and value.endswith("mxn"):
        return f"{value[:-3]}_mxn"
    return value


def _format_symbol(venue: str, symbol: str) -> str:
    if venue in {"BYBIT", "BYBIT_SPOT"}:
        compact = re.sub(r"[^A-Za-z0-9]", "", symbol or "")
        return compact.upper() or str(symbol).upper()
    if venue == "BITGET":
        return symbol.replace("_SPBL", "")
    if venue == "BITSO":
        # Bitso depth endpoints expect lowercase book ids like ``btc_mxn``.
        # Frontier observations may carry slash, dash, or compact MXN pairs
        # after venue-map normalization, so normalize them here before URL
        # construction to keep paper-only depth enrichment active for MXN books.
        return _normalize_bitso_symbol(symbol)
    if venue == "INDODAX":
        # INDODAX depth paths use compact lowercase pairs (for example
        # ``btcidr``), while ticker normalization stores ``BTC_IDR``.
        return re.sub(r"[^A-Za-z0-9]", "", symbol or "").lower()
    if venue in {"QUIDAX", "BUDA"}:
        return symbol.lower()
    if venue == "VALR":
        return symbol.replace("-", "").replace("_", "").replace("/", "").upper()
    if venue == "BITKUB":
        parts = [part for part in symbol.upper().split("_") if part]
        if len(parts) == 2:
            return f"{parts[0]}_{parts[1]}"
    return symbol


def _build_depth_url(observation: dict, depth_config: dict, levels: int) -> str:
    symbol = urllib.parse.quote(_format_symbol(str(observation["venue"]), str(observation["symbol"])), safe="-_")
    limit = min(int(depth_config.get("max_levels", levels)), levels)
    return str(depth_config["url_template"]).format(symbol=symbol, limit=limit)


def _extract_depth(parser: str, payload: object, received_at: str) -> dict:
    data = payload or {}
    bids: list = []
    asks: list = []
    book_timestamp = None
    freshness_basis = "response_received"
    if parser in {"binance_depth", "mexc_depth", "coinbase_book", "coinjar_book"}:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
    elif parser == "kucoin_level2":
        body = data.get("data") or {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("time") or body.get("timestamp"))
    elif parser == "gate_order_book":
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        book_timestamp = _timestamp_to_iso(data.get("current") or data.get("update"))
    elif parser in {"bitget_orderbook", "bybit_orderbook"}:
        body = data.get("data") or {}
        if parser == "bybit_orderbook":
            body = data.get("result") or body
        bids = body.get("bids") or body.get("b") or []
        asks = body.get("asks") or body.get("a") or []
        book_timestamp = _timestamp_to_iso(body.get("ts") or body.get("cts"))
    elif parser == "kraken_depth":
        values = list((data.get("result") or {}).values())
        body = values[0] if values else {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        timestamps = [
            _as_float(row[2])
            for row in [*bids[:1], *asks[:1]]
            if isinstance(row, (list, tuple)) and len(row) > 2
        ]
        timestamps = [value for value in timestamps if value is not None]
        if timestamps:
            book_timestamp = _timestamp_to_iso(max(timestamps), unit="seconds")
    elif parser == "okx_books":
        rows = data.get("data") or []
        body = rows[0] if rows else {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("ts"))
    elif parser in {"luno_orderbook", "valr_orderbook"}:
        bids = data.get("bids") or data.get("Bids") or []
        asks = data.get("asks") or data.get("Asks") or []
        book_timestamp = _timestamp_to_iso(
            data.get("timestamp") or data.get("Timestamp") or data.get("LastChange")
        )
    elif parser == "quidax_depth":
        body = data.get("data") or data
        bids = body.get("bids") or body.get("buy") or []
        asks = body.get("asks") or body.get("sell") or []
    elif parser == "indodax_depth":
        bids = data.get("buy") or data.get("bids") or []
        asks = data.get("sell") or data.get("asks") or []
    elif parser == "bitkub_depth":
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        bids = body.get("bids") or body.get("bid") or []
        asks = body.get("asks") or body.get("ask") or []
    elif parser == "bitso_order_book":
        body = data.get("payload") or data
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("updated_at")) or body.get("updated_at")
    elif parser in {"mercado_bitcoin_orderbook", "buda_order_book"}:
        body = data.get("order_book") if parser == "buda_order_book" else data
        body = body or {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
    elif parser == "ripio_level2":
        body = data.get("data") or {}
        bids = body.get("bids") or []
        asks = body.get("asks") or []
        book_timestamp = _timestamp_to_iso(body.get("timestamp"))
    elif parser == "whitebit_depth":
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        book_timestamp = _timestamp_to_iso(data.get("timestamp"), unit="seconds")
    else:
        raise ValueError(f"Unsupported depth parser: {parser}")
    if book_timestamp:
        freshness_basis = "exchange_timestamp"
    else:
        book_timestamp = received_at
    return {
        "bids": bids,
        "asks": asks,
        "book_timestamp": book_timestamp,
        "freshness_basis": freshness_basis,
    }


def _depth_payload_error(parser: str, payload: object) -> str | None:
    if not isinstance(payload, dict):
        return "api_error_payload:non_object"
    if parser == "bitget_orderbook":
        code = str(payload.get("code") if payload.get("code") is not None else "00000")
        if code not in {"0", "00000"}:
            return f"api_error_payload:bitget_{code[:40]}"
    if parser == "indodax_depth":
        if payload.get("error"):
            return "api_error_payload:indodax_invalid_pair"
        if payload.get("success") in {0, False, "0", "false"}:
            return "api_error_payload:indodax_unsuccessful"
    return None


def _empty_depth_reason(parser: str, payload: object) -> str:
    if parser == "bitget_orderbook" and isinstance(payload, dict):
        code = str(payload.get("code") if payload.get("code") is not None else "00000")
        if code in {"0", "00000"}:
            return "valid_empty_book"
    return "parser_empty_book"


def _should_prefer_trailing_price_quantity(
    first_price: float | None,
    first_quantity: float | None,
    trailing_price: float | None,
    trailing_quantity: float | None,
) -> bool:
    if trailing_price is None or trailing_quantity is None:
        return False
    if trailing_price <= 0 or trailing_quantity <= 0:
        return False
    if first_price is None or first_quantity is None:
        return True
    if first_price <= 0 or first_quantity <= 0:
        return True
    if first_price >= 1_000_000_000:
        return True
    return first_price < first_quantity


def _normalize_levels(raw_levels: list, side: str, max_levels: int) -> tuple[list[list[float]], list[str]]:
    valid: list[list[float]] = []
    anomalies: list[str] = []
    seen_prices: set[float] = set()
    original_prices: list[float] = []
    for raw in raw_levels[:max_levels]:
        if isinstance(raw, dict):
            price = _as_float(
                raw.get("price")
                or raw.get("rate")
                or raw.get("p")
                or raw.get("bid")
                or raw.get("ask")
            )
            quantity = _as_float(
                raw.get("volume")
                or raw.get("quantity")
                or raw.get("qty")
                or raw.get("amount")
                or raw.get("baseAmount")
            )
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            # Some venues include id/timestamp before quantity/price. If the
            # first two values do not look like price/quantity, the venue parser
            # should still leave the last two usable values in order.
            price = _as_float(raw[0])
            quantity = _as_float(raw[1])
            if len(raw) >= 4:
                alt_quantity = _as_float(raw[-2])
                alt_price = _as_float(raw[-1])
                if _should_prefer_trailing_price_quantity(price, quantity, alt_price, alt_quantity):
                    price = alt_price
                    quantity = alt_quantity
        else:
            anomalies.append("invalid_level_shape")
            continue
        if price is None or quantity is None or price <= 0 or quantity <= 0:
            anomalies.append("invalid_level_value")
            continue
        original_prices.append(price)
        if price in seen_prices:
            anomalies.append("duplicate_price_level")
            continue
        seen_prices.add(price)
        valid.append([price, quantity])
    expected = sorted(original_prices, reverse=side == "bids")
    if original_prices and original_prices != expected:
        anomalies.append("unsorted_levels")
    valid.sort(key=lambda row: row[0], reverse=side == "bids")
    return valid, sorted(set(anomalies))


def _quote_to_usd_multiplier(observation: dict) -> float | None:
    quote = str(observation.get("quote") or "").upper()
    if quote in {"USD", "USDT", "USDC", "BUSD", "DAI", "FDUSD"}:
        return 1.0
    last = _as_float(observation.get("last"))
    normalized = _as_float(observation.get("usd_normalized_last"))
    if last and normalized and last > 0 and normalized > 0:
        return normalized / last
    reference_rate = _as_float(observation.get("fx_reference_rate"))
    if reference_rate and reference_rate > 0:
        return 1.0 / reference_rate
    explicit = _as_float(observation.get("quote_to_usd_rate"))
    return explicit if explicit and explicit > 0 else None


def _depth_within(
    levels: list[list[float]],
    mid: float,
    band_bps: float,
    side: str,
    quote_to_usd: float = 1.0,
) -> float:
    if mid <= 0:
        return 0.0
    if side == "bids":
        threshold = mid * (1.0 - band_bps / 10_000.0)
        chosen = [row for row in levels if row[0] >= threshold]
    else:
        threshold = mid * (1.0 + band_bps / 10_000.0)
        chosen = [row for row in levels if row[0] <= threshold]
    return sum(price * quantity * max(0.0, quote_to_usd) for price, quantity in chosen)


def simulate_fill(
    levels: list[list[float]],
    side: str,
    notional_usd: float,
    quote_to_usd: float = 1.0,
) -> dict:
    if not levels or notional_usd <= 0 or quote_to_usd <= 0:
        return {"filled": False, "average_price": None, "slippage_bps": None, "depth_used_usd": 0.0}
    best = levels[0][0]
    remaining_quote = float(notional_usd)
    base_filled = 0.0
    quote_filled = 0.0
    for price, quantity in levels:
        level_quote_usd = price * quantity * quote_to_usd
        used_quote_usd = min(remaining_quote, level_quote_usd)
        base_filled += used_quote_usd / (price * quote_to_usd)
        quote_filled += used_quote_usd
        remaining_quote -= used_quote_usd
        if remaining_quote <= 1e-9:
            break
    if remaining_quote > max(0.01, notional_usd * 0.001) or base_filled <= 0:
        return {
            "filled": False,
            "average_price": None,
            "slippage_bps": None,
            "depth_used_usd": round(quote_filled, 3),
        }
    average = (quote_filled / quote_to_usd) / base_filled
    if side == "buy":
        slippage = (average / best - 1.0) * 10_000.0
    else:
        slippage = (1.0 - average / best) * 10_000.0
    return {
        "filled": True,
        "average_price": round(average, 12),
        "slippage_bps": round(max(0.0, slippage), 3),
        "depth_used_usd": round(quote_filled, 3),
    }


def _quality_score(
    observation: dict,
    depth: dict,
    freshness_age_seconds: float,
    freshness_basis: str,
) -> tuple[float, dict]:
    bid_depth = float(depth["depth_usd"]["bid"]["10"] or 0.0)
    ask_depth = float(depth["depth_usd"]["ask"]["10"] or 0.0)
    depth_component = min(1.0, min(bid_depth, ask_depth) / 1000.0) * 30.0
    buy_slip = depth["fills"]["buy"]["1000"].get("slippage_bps")
    sell_slip = depth["fills"]["sell"]["1000"].get("slippage_bps")
    worst_slip = max(
        [value for value in (buy_slip, sell_slip) if value is not None],
        default=100.0,
    )
    slippage_component = max(0.0, 1.0 - worst_slip / 50.0) * 25.0
    spread = float(observation.get("spread_bps") or 999.0)
    spread_component = max(0.0, 1.0 - spread / 20.0) * 20.0
    freshness_component = max(0.0, 1.0 - freshness_age_seconds / 90.0) * 15.0
    if freshness_basis == "response_received":
        freshness_component = min(freshness_component, 10.0)
    volume = float(observation.get("quote_volume_24h") or 0.0)
    depth_25 = min(
        float(depth["depth_usd"]["bid"]["25"] or 0.0),
        float(depth["depth_usd"]["ask"]["25"] or 0.0),
    )
    if volume >= 25_000 and depth_25 >= 1000:
        volume_component = 10.0
    elif volume >= 25_000 and depth_25 >= 250:
        volume_component = 7.0
    elif volume > 0 and depth_25 > 0:
        volume_component = 3.0
    else:
        volume_component = 0.0
    components = {
        "executable_depth": round(depth_component, 3),
        "simulated_slippage": round(slippage_component, 3),
        "spread": round(spread_component, 3),
        "freshness": round(freshness_component, 3),
        "volume_credibility": round(volume_component, 3),
    }
    return round(sum(components.values()), 3), components


def analyze_book(
    observation: dict,
    raw_book: dict,
    *,
    latency_ms: float,
    received_at: str,
    max_levels: int = 50,
    baseline_latency_ms: float | None = None,
    fresh_seconds: float = 30.0,
) -> dict:
    bids, bid_anomalies = _normalize_levels(raw_book.get("bids") or [], "bids", max_levels)
    asks, ask_anomalies = _normalize_levels(raw_book.get("asks") or [], "asks", max_levels)
    anomalies = [*bid_anomalies, *ask_anomalies]
    if not bids and not asks:
        anomalies.append("empty_book")
    elif not bids or not asks:
        anomalies.append("one_sided_book")
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        anomalies.append("invalid_best_prices")
        mid = float(observation.get("last") or 0.0)
    else:
        mid = (best_bid + best_ask) / 2.0
        if best_bid > best_ask:
            anomalies.append("crossed_book")
        elif best_bid == best_ask:
            anomalies.append("locked_book")
    ticker_mid = (
        (float(observation.get("bid")) + float(observation.get("ask"))) / 2.0
        if observation.get("bid") and observation.get("ask")
        else float(observation.get("last") or 0.0)
    )
    midpoint_mismatch_bps = (
        abs(mid / ticker_mid - 1.0) * 10_000.0 if mid > 0 and ticker_mid > 0 else None
    )
    if midpoint_mismatch_bps is not None and midpoint_mismatch_bps > 20.0:
        anomalies.append("ticker_book_midpoint_mismatch")
    timestamp = raw_book.get("book_timestamp") or received_at
    parsed_timestamp = _parse_iso(timestamp)
    parsed_received = _parse_iso(received_at) or dt.datetime.now(dt.timezone.utc)
    freshness_age = max(0.0, (parsed_received - parsed_timestamp).total_seconds()) if parsed_timestamp else 0.0
    if freshness_age > fresh_seconds:
        anomalies.append("stale_book")
    if latency_ms > 2000.0:
        anomalies.append("high_latency")
    if baseline_latency_ms and latency_ms > max(2000.0, baseline_latency_ms * 3.0):
        anomalies.append("latency_outlier")

    quote_to_usd = _quote_to_usd_multiplier(observation)
    if quote_to_usd is None:
        anomalies.append("missing_fx_conversion")
    depth_multiplier = float(quote_to_usd or 0.0)
    depth_usd = {
        side: {
            str(band): round(_depth_within(levels, mid, float(band), side, depth_multiplier), 3)
            for band in (5, 10, 25)
        }
        for side, levels in (("bid", bids), ("ask", asks))
    }
    fills = {"buy": {}, "sell": {}}
    for notional in QUALITY_NOTIONALS:
        fills["buy"][str(int(notional))] = simulate_fill(asks, "buy", notional, depth_multiplier)
        fills["sell"][str(int(notional))] = simulate_fill(bids, "sell", notional, depth_multiplier)
    if not fills["buy"]["1000"]["filled"] or not fills["sell"]["1000"]["filled"]:
        anomalies.append("depth_cliff")
    total_10 = depth_usd["bid"]["10"] + depth_usd["ask"]["10"]
    imbalance = (
        (depth_usd["bid"]["10"] - depth_usd["ask"]["10"]) / total_10
        if total_10 > 0
        else 0.0
    )
    all_25 = [
        price * quantity
        for levels, side in ((bids, "bids"), (asks, "asks"))
        for price, quantity in levels
        if (
            (side == "bids" and price >= mid * (1.0 - 25.0 / 10_000.0))
            or (side == "asks" and price <= mid * (1.0 + 25.0 / 10_000.0))
        )
    ]
    concentration = max(all_25) / sum(all_25) if all_25 and sum(all_25) > 0 else 1.0
    if float(observation.get("quote_volume_24h") or 0.0) >= 1_000_000 and min(
        depth_usd["bid"]["25"], depth_usd["ask"]["25"]
    ) < 100.0:
        anomalies.append("reported_volume_depth_mismatch")

    depth = {
        "bids": bids,
        "asks": asks,
        "depth_usd": depth_usd,
        "fills": fills,
        "imbalance_10bps": round(imbalance, 4),
        "depth_concentration_25bps": round(concentration, 4),
    }
    score, components = _quality_score(
        observation,
        depth,
        freshness_age,
        str(raw_book.get("freshness_basis") or "response_received"),
    )
    anomaly_flags = sorted(set(anomalies))
    critical = sorted(CRITICAL_ANOMALIES.intersection(anomaly_flags))
    status = "unknown" if quote_to_usd is None else "verified" if not anomaly_flags else "degraded"
    return {
        "quality_status": status,
        "quality_score": score,
        "quality_components": components,
        "book_timestamp": timestamp,
        "book_observed_at": received_at,
        "freshness_basis": raw_book.get("freshness_basis") or "response_received",
        "freshness_age_seconds": round(freshness_age, 3),
        "depth_latency_ms": round(latency_ms, 3),
        "book_mid": round(mid, 12) if mid else None,
        "ticker_book_midpoint_mismatch_bps": (
            round(midpoint_mismatch_bps, 3) if midpoint_mismatch_bps is not None else None
        ),
        "book_levels": {"bids": bids, "asks": asks},
        "depth_usd": depth_usd,
        "simulated_fills": fills,
        "book_imbalance_10bps": depth["imbalance_10bps"],
        "depth_concentration_25bps": depth["depth_concentration_25bps"],
        "quote_to_usd_multiplier": round(quote_to_usd, 12) if quote_to_usd is not None else None,
        "anomaly_flags": anomaly_flags,
        "critical_anomaly_flags": critical,
    }


def _unknown_quality(observation: dict, result: dict | None, reason: str) -> dict:
    status = (result or {}).get("status") or "unknown"
    return {
        "quality_status": "blocked" if status == "blocked" else "unknown",
        "quality_score": None,
        "quality_components": {},
        "book_timestamp": None,
        "book_observed_at": (result or {}).get("received_at") or _utc_now(),
        "freshness_basis": "unavailable",
        "freshness_age_seconds": None,
        "depth_latency_ms": (result or {}).get("latency_ms"),
        "book_mid": None,
        "ticker_book_midpoint_mismatch_bps": None,
        "book_levels": {"bids": [], "asks": []},
        "depth_usd": {"bid": {"5": 0.0, "10": 0.0, "25": 0.0}, "ask": {"5": 0.0, "10": 0.0, "25": 0.0}},
        "simulated_fills": {
            side: {
                str(int(notional)): {
                    "filled": False,
                    "average_price": None,
                    "slippage_bps": None,
                    "depth_used_usd": 0.0,
                }
                for notional in QUALITY_NOTIONALS
            }
            for side in ("buy", "sell")
        },
        "book_imbalance_10bps": None,
        "depth_concentration_25bps": None,
        "anomaly_flags": [reason],
        "critical_anomaly_flags": [],
        "depth_http_status": (result or {}).get("http_status"),
    }


def _venue_depth_targets(registry: dict) -> dict[str, dict]:
    return {
        str(row["venue"]): row
        for row in registry.get("venues", [])
        if row.get("enabled", True) and isinstance(row.get("depth"), dict)
    }


def _snapshot_rotation_state(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        select inst_id, observed_at, quality_status, anomaly_json
        from frontier_quality_snapshots
        order by inst_id asc, observed_at desc
        """
    ).fetchall()
    state: dict[str, dict] = {}
    for row in rows:
        inst_id = str(row["inst_id"])
        item = state.setdefault(
            inst_id,
            {
                "last_observed_at": row["observed_at"],
                "consecutive_verified_count": 0,
                "counting": True,
                "consecutive_valid_empty_count": 0,
                "empty_counting": True,
            },
        )
        if item["counting"] and row["quality_status"] == "verified":
            item["consecutive_verified_count"] += 1
        else:
            item["counting"] = False
        try:
            anomalies = set(json.loads(row["anomaly_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            anomalies = set()
        if item["empty_counting"] and "valid_empty_book" in anomalies:
            item["consecutive_valid_empty_count"] += 1
        else:
            item["empty_counting"] = False
    return state


def _snapshot_sort_key(row: dict, state: dict[str, dict], cfg: dict | None = None) -> tuple:
    inst_id = str(row.get("instrument_id"))
    item = state.get(inst_id, {})
    verified_count = int(item.get("consecutive_verified_count") or 0)
    empty_count = int(item.get("consecutive_valid_empty_count") or 0)
    last_seen = item.get("last_observed_at") or ""
    cfg = cfg or {}
    empty_threshold = int(cfg.get("valid_empty_book_cooldown_after", 2))
    core_assets = {str(value).upper() for value in cfg.get("empty_book_core_asset_exemptions", ["BTC", "ETH", "USDT", "USDC"])}
    cooled_down = empty_count >= empty_threshold and str(row.get("base") or "").upper() not in core_assets
    return (1 if cooled_down else 0, verified_count, last_seen, -float(row.get("quote_volume_24h") or 0.0))


def _known_quality_rate_from_state(observations: list[dict], state: dict[str, dict]) -> float:
    reachable = [
        row
        for row in observations
        if row.get("instrument_id") and row.get("data_status") == "reachable"
    ]
    if not reachable:
        return 0.0
    known = sum(
        1
        for row in reachable
        if int((state.get(str(row.get("instrument_id"))) or {}).get("consecutive_verified_count") or 0) > 0
    )
    return known / len(reachable)


def _quality_target_escalation(
    observations: list[dict],
    snapshot_state: dict[str, dict],
    cfg: dict,
    base_max_total: int,
    base_max_per_venue: int,
) -> dict:
    target = float(cfg.get("known_quality_rate_target", 0.25))
    reachable_count = sum(1 for row in observations if row.get("instrument_id") and row.get("data_status") == "reachable")
    historical = _known_quality_rate_from_state(observations, snapshot_state)
    current_cycle = min(1.0, base_max_total / reachable_count) if reachable_count else 0.0
    current = min(historical, current_cycle)
    enabled = bool(cfg.get("quality_target_escalation_enabled", False))
    if not enabled or not observations or current >= target:
        return {
            "enabled": enabled,
            "active": False,
            "known_quality_rate_before_selection": round(current, 4),
            "historical_known_quality_rate": round(historical, 4),
            "current_cycle_quality_rate_at_base_budget": round(current_cycle, 4),
            "known_quality_rate_target": round(target, 4),
            "max_symbols_per_cycle": base_max_total,
            "max_symbols_per_venue": base_max_per_venue,
            "extra_symbols_requested": 0,
            "starved_venue_reserve": 0,
        }
    extra = int(cfg.get("quality_target_extra_symbols_per_cycle", 0) or 0)
    max_total = min(int(cfg.get("quality_target_max_symbols_per_cycle", base_max_total + extra)), base_max_total + extra)
    max_per_venue = max(
        base_max_per_venue,
        min(
            int(cfg.get("quality_target_max_symbols_per_venue", base_max_per_venue)),
            base_max_per_venue + max(1, min(extra, extra // 6 if extra >= 6 else extra)),
        ),
    )
    return {
        "enabled": enabled,
        "active": True,
        "known_quality_rate_before_selection": round(current, 4),
        "historical_known_quality_rate": round(historical, 4),
        "current_cycle_quality_rate_at_base_budget": round(current_cycle, 4),
        "known_quality_rate_target": round(target, 4),
        "max_symbols_per_cycle": max_total,
        "max_symbols_per_venue": max_per_venue,
        "extra_symbols_requested": max(0, max_total - base_max_total),
        "starved_venue_reserve": int(cfg.get("quality_target_starved_venue_reserve_per_cycle", 0) or 0),
    }


def _starved_sort_key(row: dict, state: dict[str, dict], starved_venues: set[str], cfg: dict | None = None) -> tuple:
    venue = str(row.get("venue") or "").upper()
    return (0 if venue in starved_venues else 1, *_snapshot_sort_key(row, state, cfg))


def _venue_depth_minimum_targets(cfg: dict, starved_venues: set[str]) -> dict[str, int]:
    fallback = max(0, int(cfg.get("starved_venue_min_depth_per_cycle", 0) or 0))
    configured = cfg.get("venue_depth_minimums", {}) or {}
    targets = {venue: fallback for venue in starved_venues if fallback > 0}
    for venue, value in configured.items():
        target = max(0, int(value or 0))
        if target > 0:
            targets[str(venue).upper()] = target
    return targets


def _venue_count(counts: collections.Counter[str], venue: str) -> int:
    venue_upper = str(venue).upper()
    return sum(count for key, count in counts.items() if str(key).upper() == venue_upper)


def _selection_quota_report(
    observations: list[dict],
    selected: list[dict],
    snapshot_state: dict[str, dict],
    targets: dict[str, int],
    max_total: int,
    max_per_venue: int,
) -> dict[str, dict]:
    observed: collections.Counter[str] = collections.Counter()
    reachable: collections.Counter[str] = collections.Counter()
    selected_counts: collections.Counter[str] = collections.Counter()
    known_prior: collections.Counter[str] = collections.Counter()
    for row in observations:
        venue = str(row.get("venue") or "unknown").upper()
        if venue not in targets:
            continue
        if row.get("instrument_id"):
            observed[venue] += 1
        if row.get("instrument_id") and row.get("data_status") == "reachable":
            reachable[venue] += 1
            state = snapshot_state.get(str(row.get("instrument_id"))) or {}
            if int(state.get("consecutive_verified_count") or 0) > 0:
                known_prior[venue] += 1
    for row in selected:
        venue = str(row.get("venue") or "unknown").upper()
        if venue in targets:
            selected_counts[venue] += 1

    report: dict[str, dict] = {}
    total_selected = len(selected)
    for venue, target in sorted(targets.items()):
        observed_count = int(observed.get(venue, 0))
        reachable_count = int(reachable.get(venue, 0))
        selected_count = int(selected_counts.get(venue, 0))
        required = min(target, reachable_count, max_per_venue)
        if observed_count == 0:
            status = "missed"
            missed_reason = "no_observations"
        elif reachable_count == 0:
            status = "missed"
            missed_reason = "no_reachable_observations"
        elif selected_count >= required:
            status = "met"
            missed_reason = None
        elif observed_count < target or reachable_count < target:
            status = "partial"
            missed_reason = "insufficient_reachable_observations"
        elif selected_count >= max_per_venue:
            status = "partial"
            missed_reason = "per_venue_cap"
        elif total_selected >= max_total:
            status = "partial"
            missed_reason = "total_cycle_cap"
        elif known_prior.get(venue, 0) >= reachable_count:
            status = "partial"
            missed_reason = "already_verified_or_lower_priority"
        else:
            status = "partial"
            missed_reason = "unfilled_after_priority_selection"
        report[venue] = {
            "target_selected_this_cycle": int(target),
            "target_after_caps": int(required),
            "observed_count": observed_count,
            "reachable_count": reachable_count,
            "selected_this_cycle": selected_count,
            "previously_verified_reachable_count": int(known_prior.get(venue, 0)),
            "status": status,
            "missed_reason": missed_reason,
        }
    return report


def _exploit_more_market_keys(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            """
            select market_key
            from market_hunter_directives
            where status = 'open' and directive = 'exploit_more'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["market_key"]) for row in rows if row["market_key"]}


def _open_growth_market_keys(conn: sqlite3.Connection) -> set[str]:
    keys: set[str] = set()
    try:
        rows = conn.execute(
            """
            select signal_key
            from growth_experiments
            where status = 'open' and priority >= 70
            """
        ).fetchall()
        keys.update(str(row["signal_key"]) for row in rows if row["signal_key"])
    except sqlite3.OperationalError:
        pass
    try:
        rows = conn.execute(
            """
            select title, rationale
            from improvement_tasks
            where status = 'open' and priority >= 60
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = f"{row['title']} {row['rationale']}"
        for token in re.findall(r"[A-Z0-9_]+\|frontier_crypto_venue_map\|(?:long|short)_frontier_spot\|(?:standard|conditional)", text):
            keys.add(token)
    return keys


def _candidate_market_keys(row: dict) -> set[str]:
    venue = str(row.get("venue") or "")
    trade_type = str(row.get("trade_type") or "frontier_crypto_venue_map")
    direction = str(row.get("direction") or "")
    route_status = str((row.get("execution_feasibility") or {}).get("status") or "")
    keys = {str(row.get("signal_key") or ""), str(row.get("market_key") or "")}
    if venue and trade_type and direction and route_status:
        keys.add(f"{venue}|{trade_type}|{direction}|{route_status}")
    if venue and trade_type and direction:
        keys.add(f"{venue}|{trade_type}|{direction}")
    if venue and direction:
        keys.add(f"{venue}|{direction}")
    return {key for key in keys if key}


def select_enrichment_observations(
    conn: sqlite3.Connection,
    observations: list[dict],
    variants: list[dict],
    candidates_by_variant: dict[str, list[dict]],
    settings: dict,
) -> list[dict]:
    cfg = settings.get("frontier_data_quality", {})
    base_max_total = int(cfg.get("max_symbols_per_cycle", 60))
    base_max_per_venue = int(cfg.get("max_symbols_per_venue", 12))
    unknown_reserve = int(cfg.get("unknown_quality_reserve_per_cycle", 30))
    regional_reserve = int(cfg.get("regional_reserve_per_cycle", 25))
    exploit_variant_reserve = int(cfg.get("exploit_variant_reserve_per_cycle", 25))
    active_variant_top = int(cfg.get("active_variant_enrichment_top", 10))
    shadow_variant_top = int(cfg.get("shadow_variant_enrichment_top", 2))
    shadow_variant_cap = int(cfg.get("shadow_variant_enrichment_variant_cap", 8))
    starved_venues = {str(venue).upper() for venue in cfg.get("starved_venues", [])}
    venue_minimum_targets = _venue_depth_minimum_targets(cfg, starved_venues)
    adaptive = bool(cfg.get("adaptive_selection", True))
    snapshot_state = _snapshot_rotation_state(conn) if adaptive else {}
    escalation = _quality_target_escalation(
        observations,
        snapshot_state,
        cfg,
        base_max_total,
        base_max_per_venue,
    )
    max_total = int(escalation["max_symbols_per_cycle"])
    max_per_venue = int(escalation["max_symbols_per_venue"])
    if escalation["active"]:
        unknown_reserve += max(0, int(escalation["extra_symbols_requested"]) // 2)
        regional_reserve += max(0, int(escalation["extra_symbols_requested"]) // 4)
    by_id = {str(row.get("instrument_id")): row for row in observations if row.get("instrument_id")}
    selected: list[dict] = []
    selected_ids: set[str] = set()
    venue_counts: collections.Counter[str] = collections.Counter()

    def add_ids(inst_ids: list[str], bucket: str, bucket_limit: int | None = None) -> None:
        added = 0
        for inst_id in inst_ids:
            if len(selected) >= max_total:
                return
            if bucket_limit is not None and added >= bucket_limit:
                return
            row = by_id.get(str(inst_id))
            if not row or str(inst_id) in selected_ids or row.get("data_status") != "reachable":
                continue
            venue = str(row.get("venue"))
            if venue_counts[venue] >= max_per_venue:
                continue
            output = dict(row)
            output["depth_selection_bucket"] = bucket
            output["starved_venue"] = venue.upper() in starved_venues
            output["depth_selection_escalation"] = escalation
            selected.append(output)
            selected_ids.add(str(inst_id))
            venue_counts[venue] += 1
            added += 1

    open_rows = conn.execute(
        """
        select distinct inst_id
        from paper_trades
        where status = 'open' and trade_type = 'frontier_crypto_venue_map'
        order by opened_at asc
        """
    ).fetchall()
    add_ids([str(row["inst_id"]) for row in open_rows], "open_paper_trade")

    exploit_keys = _exploit_more_market_keys(conn) | _open_growth_market_keys(conn)
    active = next((item for item in variants if item.get("status") == "active"), None)
    exploit_variant_ids: list[str] = []
    if active:
        exploit_variant_ids.extend(
            str(row["inst_id"])
            for row in candidates_by_variant.get(active["variant_id"], [])[:active_variant_top]
            if row.get("inst_id")
        )
    shadow_variants_used = 0
    for variant in variants:
        if variant.get("status") not in {"shadow", "retired"}:
            continue
        if variant.get("status") == "shadow" and shadow_variants_used >= shadow_variant_cap:
            continue
        if variant.get("status") == "shadow":
            shadow_variants_used += 1
        exploit_variant_ids.extend(
            str(row["inst_id"])
            for row in candidates_by_variant.get(variant["variant_id"], [])[:shadow_variant_top]
            if row.get("inst_id")
        )
    raw_candidates = [
        row
        for rows in candidates_by_variant.values()
        for row in rows
        if row.get("inst_id")
    ]
    raw_ranked = sorted(
        raw_candidates,
        key=lambda row: abs(float(row.get("venue_deviation_bps") or 0.0)),
        reverse=True,
    )
    exploit_ranked = [
        row
        for row in raw_ranked
        if exploit_keys.intersection(_candidate_market_keys(row))
    ]
    exploit_variant_ids.extend(str(row["inst_id"]) for row in exploit_ranked if row.get("inst_id"))
    add_ids(exploit_variant_ids, "exploit_more_or_variant", exploit_variant_reserve)

    regional_ranked = sorted(
        [row for row in observations if row.get("region") and row.get("instrument_id")],
        key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues, cfg)
        if adaptive
        else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
    )
    add_ids([str(row["instrument_id"]) for row in regional_ranked], "regional_frontier", regional_reserve)

    broad_unknown_ranked = sorted(
        [
            row
            for row in observations
            if row.get("instrument_id")
            and row.get("data_status") == "reachable"
            and float(row.get("quote_volume_24h") or 0.0) > 0
            and int((snapshot_state.get(str(row.get("instrument_id"))) or {}).get("consecutive_verified_count") or 0) == 0
        ],
        key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues, cfg)
        if adaptive
        else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
    )
    add_ids([str(row["instrument_id"]) for row in broad_unknown_ranked], "unknown_quality_high_volume", unknown_reserve)

    for venue, target in sorted(venue_minimum_targets.items()):
        needed = max(0, target - _venue_count(venue_counts, venue))
        if needed <= 0:
            continue
        venue_ranked = sorted(
            [
                row
                for row in observations
                if row.get("instrument_id")
                and row.get("data_status") == "reachable"
                and str(row.get("venue") or "").upper() == venue
            ],
            key=lambda row: _snapshot_sort_key(row, snapshot_state, cfg)
            if adaptive
            else -float(row.get("quote_volume_24h") or 0.0),
        )
        add_ids([str(row["instrument_id"]) for row in venue_ranked], "starved_venue_minimum", needed)

    if escalation["active"] and starved_venues:
        starved_ranked = sorted(
            [
                row
                for row in observations
                if row.get("instrument_id")
                and row.get("data_status") == "reachable"
                and str(row.get("venue") or "").upper() in starved_venues
            ],
            key=lambda row: _snapshot_sort_key(row, snapshot_state, cfg)
            if adaptive
            else -float(row.get("quote_volume_24h") or 0.0),
        )
        add_ids(
            [str(row["instrument_id"]) for row in starved_ranked],
            "quality_target_starved_venue",
            int(escalation.get("starved_venue_reserve") or 0),
        )

    add_ids([str(row["inst_id"]) for row in raw_ranked if row.get("inst_id")], "largest_dislocation")
    add_ids(
        [
            str(row["instrument_id"])
            for row in sorted(
                [
                    row
                    for row in observations
                    if row.get("instrument_id")
                    and row.get("data_status") == "reachable"
                    and float(row.get("quote_volume_24h") or 0.0) > 0
                ],
                key=lambda row: _starved_sort_key(row, snapshot_state, starved_venues, cfg)
                if adaptive
                else (0 if str(row.get("venue") or "").upper() in starved_venues else 1, -float(row.get("quote_volume_24h") or 0.0)),
            )
        ],
        "rotation_fill",
    )
    quota_report = _selection_quota_report(
        observations,
        selected,
        snapshot_state,
        venue_minimum_targets,
        max_total,
        max_per_venue,
    )
    selection_limits = {
        "max_symbols_per_cycle": max_total,
        "max_symbols_per_venue": max_per_venue,
        "base_max_symbols_per_cycle": base_max_total,
        "base_max_symbols_per_venue": base_max_per_venue,
        "unknown_quality_reserve_per_cycle": unknown_reserve,
        "regional_reserve_per_cycle": regional_reserve,
        "exploit_variant_reserve_per_cycle": exploit_variant_reserve,
    }
    for row in selected:
        row["depth_selection_venue_quota_report"] = quota_report
        row["depth_selection_limits"] = selection_limits
    return selected


def _rolling_latency_baselines(conn: sqlite3.Connection) -> dict[str, float]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """
        select venue, latency_ms
        from frontier_quality_snapshots
        where observed_at >= ? and latency_ms is not null
        """,
        (cutoff,),
    ).fetchall()
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row["venue"])].append(float(row["latency_ms"]))
    return {venue: statistics.median(values) for venue, values in grouped.items() if values}


def enrich_observations(
    conn: sqlite3.Connection,
    observations: list[dict],
    selected: list[dict],
    settings: dict,
    registry: dict,
) -> tuple[list[dict], dict]:
    cfg = settings.get("frontier_data_quality", {})
    if not cfg.get("enabled", True):
        return observations, {"enabled": False}
    targets = _venue_depth_targets(registry)
    timeout = int(cfg.get("timeout_seconds", 6))
    levels = int(cfg.get("depth_levels", 50))
    baselines = _rolling_latency_baselines(conn)
    results: dict[str, dict] = {}

    def fetch_one(observation: dict) -> tuple[str, dict]:
        inst_id = str(observation["instrument_id"])
        target = targets.get(str(observation.get("venue")))
        if not target:
            return inst_id, _unknown_quality(observation, None, "depth_endpoint_not_configured")
        depth_config = target["depth"]
        try:
            url = _build_depth_url(observation, depth_config, levels)
            result = _fetch_json(url, timeout)
            if not result["ok"]:
                return inst_id, _unknown_quality(observation, result, f"depth_{result['status']}")
            payload_error = _depth_payload_error(str(depth_config["parser"]), result["payload"])
            if payload_error:
                return inst_id, _unknown_quality(observation, result, payload_error)
            extracted = _extract_depth(str(depth_config["parser"]), result["payload"], result["received_at"])
            if not extracted.get("bids") and not extracted.get("asks"):
                return inst_id, _unknown_quality(
                    observation,
                    result,
                    _empty_depth_reason(str(depth_config["parser"]), result["payload"]),
                )
            quality = analyze_book(
                observation,
                extracted,
                latency_ms=float(result["latency_ms"]),
                received_at=str(result["received_at"]),
                max_levels=min(levels, int(depth_config.get("max_levels", levels))),
                baseline_latency_ms=baselines.get(str(observation.get("venue"))),
                fresh_seconds=float(cfg.get("fresh_seconds", 30.0)),
            )
            quality["depth_source_url"] = url
            quality["depth_http_status"] = result["http_status"]
            quality["depth_parser"] = depth_config["parser"]
            return inst_id, quality
        except Exception as exc:  # noqa: BLE001
            return inst_id, _unknown_quality(observation, None, f"depth_enrichment_error:{str(exc)[:120]}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(cfg.get("workers", 8))) as pool:
        futures = [pool.submit(fetch_one, row) for row in selected]
        for future in concurrent.futures.as_completed(futures):
            inst_id, quality = future.result()
            results[inst_id] = quality

    snapshot_state = _snapshot_rotation_state(conn)
    enriched = []
    selected_ids = {str(row["instrument_id"]) for row in selected}
    bucket_counts = collections.Counter(str(row.get("depth_selection_bucket") or "unclassified") for row in selected)
    selected_by_venue = collections.Counter(str(row.get("venue") or "unknown") for row in selected)
    selection_escalation = selected[0].get("depth_selection_escalation", {}) if selected else {}
    selection_quota_report = {
        str(venue): dict(item)
        for venue, item in (selected[0].get("depth_selection_venue_quota_report", {}) if selected else {}).items()
    }
    selection_limits = selected[0].get("depth_selection_limits", {}) if selected else {}
    starved_venues = {
        str(venue).upper()
        for venue in cfg.get("starved_venues", [])
    }
    for row in observations:
        output = dict(row)
        inst_id = str(row.get("instrument_id"))
        quality = results.get(str(row.get("instrument_id")))
        if quality:
            output.update(quality)
        elif inst_id in selected_ids:
            output.update(_unknown_quality(row, None, "depth_result_missing"))
        else:
            output.update(_unknown_quality(row, None, "not_selected_for_depth"))
        prior_count = int((snapshot_state.get(inst_id) or {}).get("consecutive_verified_count") or 0)
        output["verified_depth_snapshot_count"] = (
            prior_count + 1 if output.get("quality_status") == "verified" and inst_id in selected_ids else prior_count
        )
        enriched.append(output)
    target_venues = {str(venue).upper() for venue in targets}
    result_issues_by_venue: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    result_quality_by_venue: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in selected:
        venue = str(row.get("venue") or "unknown").upper()
        quality = results.get(str(row.get("instrument_id"))) or {}
        result_quality_by_venue[venue][str(quality.get("quality_status") or "missing")] += 1
        for flag in quality.get("anomaly_flags") or []:
            if str(flag).startswith("depth_") or flag in {"depth_endpoint_not_configured", "depth_result_missing"}:
                result_issues_by_venue[venue][str(flag)] += 1
    for venue, item in selection_quota_report.items():
        item["depth_endpoint_configured"] = str(venue).upper() in target_venues
        item["selected_quality_status_counts"] = dict(result_quality_by_venue.get(str(venue).upper(), {}))
        item["depth_issue_counts"] = dict(result_issues_by_venue.get(str(venue).upper(), {}))
        if not item["depth_endpoint_configured"] and item.get("observed_count", 0) > 0:
            item["status"] = "missed"
            item["missed_reason"] = "no_depth_endpoint"
    return enriched, {
        "enabled": True,
        "selected_count": len(selected),
        "enriched_count": sum(1 for item in results.values() if item.get("quality_status") in {"verified", "degraded"}),
        "unknown_count": sum(1 for item in results.values() if item.get("quality_status") == "unknown"),
        "blocked_count": sum(1 for item in results.values() if item.get("quality_status") == "blocked"),
        "selected_instruments": [row.get("instrument_id") for row in selected],
        "selection_escalation": selection_escalation,
        "selection_limits": selection_limits,
        "worker_count": int(cfg.get("workers", 8)),
        "venue_quota_report": selection_quota_report,
        "selection_bucket_counts": dict(bucket_counts),
        "selected_by_venue": dict(selected_by_venue),
        "starved_venues": sorted(starved_venues),
        "selected_starved_venue_count": sum(
            count
            for venue, count in selected_by_venue.items()
            if str(venue).upper() in starved_venues
        ),
        "starved_selected_by_venue": {
            venue: count
            for venue, count in sorted(selected_by_venue.items())
            if str(venue).upper() in starved_venues
        },
    }


def persist_quality_snapshots(
    conn: sqlite3.Connection,
    observations: list[dict],
    settings: dict,
) -> dict:
    cfg = settings.get("frontier_data_quality", {})
    now = dt.datetime.now(dt.timezone.utc)
    bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).isoformat()
    inserted = 0
    for row in observations:
        if "not_selected_for_depth" in (row.get("anomaly_flags") or []):
            continue
        if row.get("quality_status") not in {"verified", "degraded", "unknown", "blocked"}:
            continue
        fills = row.get("simulated_fills") or {}
        depth = row.get("depth_usd") or {}
        conn.execute(
            """
            insert into frontier_quality_snapshots (
                bucket_at, observed_at, venue, inst_id, quality_status,
                quality_score, venue_quality_score, latency_ms,
                freshness_age_seconds, spread_bps, bid_depth_10bps_usd,
                ask_depth_10bps_usd, buy_slippage_1000_bps,
                sell_slippage_1000_bps, anomaly_json, metrics_json
            ) values (?, ?, ?, ?, ?, ?, null, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(bucket_at, inst_id) do update set
                observed_at = excluded.observed_at,
                quality_status = excluded.quality_status,
                quality_score = excluded.quality_score,
                latency_ms = excluded.latency_ms,
                freshness_age_seconds = excluded.freshness_age_seconds,
                spread_bps = excluded.spread_bps,
                bid_depth_10bps_usd = excluded.bid_depth_10bps_usd,
                ask_depth_10bps_usd = excluded.ask_depth_10bps_usd,
                buy_slippage_1000_bps = excluded.buy_slippage_1000_bps,
                sell_slippage_1000_bps = excluded.sell_slippage_1000_bps,
                anomaly_json = excluded.anomaly_json,
                metrics_json = excluded.metrics_json
            """,
            (
                bucket,
                row.get("book_observed_at") or _utc_now(),
                row.get("venue"),
                row.get("instrument_id"),
                row.get("quality_status"),
                row.get("quality_score"),
                row.get("depth_latency_ms"),
                row.get("freshness_age_seconds"),
                row.get("spread_bps"),
                ((depth.get("bid") or {}).get("10")),
                ((depth.get("ask") or {}).get("10")),
                (((fills.get("buy") or {}).get("1000") or {}).get("slippage_bps")),
                (((fills.get("sell") or {}).get("1000") or {}).get("slippage_bps")),
                json.dumps(row.get("anomaly_flags") or [], sort_keys=True),
                json.dumps(
                    {
                        "quality_components": row.get("quality_components") or {},
                        "depth_usd": depth,
                        "simulated_fills": fills,
                        "freshness_basis": row.get("freshness_basis"),
                        "depth_concentration_25bps": row.get("depth_concentration_25bps"),
                        "book_imbalance_10bps": row.get("book_imbalance_10bps"),
                    },
                    sort_keys=True,
                ),
            ),
        )
        inserted += 1
    max_rows = int(cfg.get("snapshot_retention_rows", 100000))
    total = int(conn.execute("select count(*) as n from frontier_quality_snapshots").fetchone()["n"])
    deleted = 0
    if total > max_rows:
        deleted = total - max_rows
        conn.execute(
            """
            delete from frontier_quality_snapshots
            where id in (
                select id from frontier_quality_snapshots
                order by bucket_at asc, id asc
                limit ?
            )
            """,
            (deleted,),
        )
    conn.commit()
    return {"snapshot_rows_written": inserted, "snapshot_rows_deleted": deleted, "retention_limit": max_rows}


def venue_quality_scores(conn: sqlite3.Connection) -> list[dict]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """
        select venue, quality_status, quality_score, latency_ms,
               freshness_age_seconds, spread_bps, anomaly_json
        from frontier_quality_snapshots
        where observed_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        grouped[str(row["venue"])].append(dict(row))
    output = []
    for venue, items in grouped.items():
        reachable = sum(item["quality_status"] in {"verified", "degraded"} for item in items) / len(items)
        freshness = statistics.fmean(
            max(
                0.0,
                1.0
                - float(
                    item["freshness_age_seconds"]
                    if item["freshness_age_seconds"] is not None
                    else 90.0
                )
                / 90.0,
            )
            for item in items
        )
        scores = [float(item["quality_score"]) for item in items if item["quality_score"] is not None]
        median_quality = statistics.median(scores) / 100.0 if scores else 0.0
        anomaly_free = sum(not json.loads(item["anomaly_json"] or "[]") for item in items) / len(items)
        latencies = [float(item["latency_ms"]) for item in items if item["latency_ms"] is not None]
        if latencies:
            median_latency = statistics.median(latencies)
            dispersion = statistics.pstdev(latencies) if len(latencies) > 1 else 0.0
            latency_stability = max(0.0, 1.0 - dispersion / max(median_latency, 1.0))
        else:
            median_latency = None
            latency_stability = 0.0
        spreads = [
            float(item["spread_bps"])
            for item in items
            if item["spread_bps"] is not None and float(item["spread_bps"]) >= 0.0
        ]
        if spreads:
            median_spread = statistics.median(spreads)
            spread_dispersion = statistics.pstdev(spreads) if len(spreads) > 1 else 0.0
            spread_stability = max(
                0.0,
                1.0 - spread_dispersion / max(median_spread, 1.0),
            )
            spread_sanity = max(0.0, 1.0 - median_spread / 20.0)
            spread_health = (spread_stability + spread_sanity) / 2.0
        else:
            median_spread = None
            spread_dispersion = None
            spread_stability = 0.0
            spread_health = 0.0
        score = (
            reachable * 25.0
            + freshness * 20.0
            + median_quality * 20.0
            + anomaly_free * 10.0
            + latency_stability * 10.0
            + spread_health * 15.0
        )
        if len(spreads) > 1 and spread_stability < 0.5:
            score = min(score, 59.0)
        output.append(
            {
                "venue": venue,
                "venue_quality_score": round(score, 3),
                "snapshot_count": len(items),
                "reachability_rate": round(reachable, 3),
                "freshness_score": round(freshness, 3),
                "median_instrument_quality": round(median_quality * 100.0, 3),
                "anomaly_free_rate": round(anomaly_free, 3),
                "median_latency_ms": round(median_latency, 3) if median_latency is not None else None,
                "latency_stability": round(latency_stability, 3),
                "median_spread_bps": round(median_spread, 3) if median_spread is not None else None,
                "spread_dispersion_bps": (
                    round(spread_dispersion, 3) if spread_dispersion is not None else None
                ),
                "spread_stability_score": round(spread_stability, 3),
                "spread_health_score": round(spread_health, 3),
            }
        )
    output.sort(key=lambda item: item["venue_quality_score"], reverse=True)
    return output


def annotate_venue_quality_scores(
    observations: list[dict],
    leaderboard: list[dict],
) -> list[dict]:
    """Attach rolling venue-health telemetry to frontier observations."""

    by_venue = {
        str(item.get("venue") or "").upper(): item
        for item in leaderboard
        if item.get("venue")
    }
    for observation in observations:
        health = by_venue.get(str(observation.get("venue") or "").upper())
        observation["venue_health"] = dict(health) if health else None
        observation["venue_health_score"] = (
            health.get("venue_quality_score") if health else None
        )
    return observations


def market_testing_progress(conn: sqlite3.Connection) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    windows = {
        "last_hour": now - dt.timedelta(hours=1),
        "last_24h": now - dt.timedelta(hours=24),
    }
    output: dict[str, dict] = {}
    for label, cutoff in windows.items():
        rows = conn.execute(
            """
            select venue, inst_id, quality_status
            from frontier_quality_snapshots
            where observed_at >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        venues = {str(row["venue"]) for row in rows}
        instruments = {str(row["inst_id"]) for row in rows}
        known = {
            str(row["inst_id"])
            for row in rows
            if row["quality_status"] in {"verified", "degraded"}
        }
        new_market_rows = conn.execute(
            """
            select venue, inst_id, min(observed_at) as first_seen
            from frontier_quality_snapshots
            group by venue, inst_id
            having first_seen >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        new_venue_rows = conn.execute(
            """
            select venue, min(observed_at) as first_seen
            from frontier_quality_snapshots
            group by venue
            having first_seen >= ?
            """,
            (cutoff.isoformat(),),
        ).fetchall()
        output[label] = {
            "venues_tested": len(venues),
            "markets_tested": len(instruments),
            "new_venues_tested": len({str(row["venue"]) for row in new_venue_rows}),
            "new_markets_tested": len({f"{row['venue']}:{row['inst_id']}" for row in new_market_rows}),
            "known_quality_markets": len(known),
            "quality_test_count": len(rows),
        }
    return output


def quality_outcome_relationship(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select o.pnl_bps, p.candidate_json
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where o.horizon_minutes = 60
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
          and p.trade_type = 'frontier_crypto_venue_map'
        """
    ).fetchall()
    grouped: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        candidate = json.loads(row["candidate_json"] or "{}")
        score = candidate.get("quality_score")
        if score is None:
            continue
        score = float(score)
        bucket = "0-34" if score < 35 else "35-59" if score < 60 else "60-79" if score < 80 else "80-100"
        grouped[bucket].append(float(row["pnl_bps"]))
    output = []
    for bucket in ("0-34", "35-59", "60-79", "80-100"):
        values = grouped.get(bucket, [])
        if not values:
            continue
        output.append(
            {
                "quality_bucket": bucket,
                "closed_count": len(values),
                "avg_pnl_bps": round(statistics.fmean(values), 3),
                "win_rate": round(sum(value > 0 for value in values) / len(values), 3),
            }
        )
    return output
