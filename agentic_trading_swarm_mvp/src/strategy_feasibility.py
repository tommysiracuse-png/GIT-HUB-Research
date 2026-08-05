"""Feasibility profiling and bounded paper-only relaxation for Strategy Lab."""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
import sqlite3
import statistics
from typing import Any

from strategy_program import (
    ProgramValidationError,
    SAFE_FUNCTIONS,
    _program_values,
    _universe_matches,
    compile_observation_program,
    evaluate_expression,
)


IMMUTABLE_GATE_FIELDS = {
    "last",
    "stale_minutes",
    "session_status",
    "data_status",
    "route_status",
}


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * max(0.0, min(1.0, fraction))
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _top_level_gates(expression: str) -> list[ast.expr]:
    parsed = ast.parse(expression, mode="eval").body
    if isinstance(parsed, ast.BoolOp) and isinstance(parsed.op, ast.And):
        return list(parsed.values)
    return [parsed]


def _gate_names(node: ast.AST) -> set[str]:
    return {
        item.id for item in ast.walk(node)
        if isinstance(item, ast.Name) and item.id not in SAFE_FUNCTIONS
    }


def _numeric_compare(node: ast.AST) -> tuple[ast.expr, ast.cmpop, float] | None:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return None
    right = node.comparators[0]
    if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
        return node.left, node.ops[0], float(right.value)
    return None


def _feature_profile(values_rows: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    keys = sorted({key for row in values_rows for key in row})
    for key in keys:
        numbers = [number for row in values_rows if (number := _finite(row.get(key))) is not None]
        if not numbers:
            continue
        result[key] = {
            "count": len(numbers),
            "min": round(min(numbers), 6),
            "p25": round(_percentile(numbers, 0.25), 6),
            "median": round(statistics.median(numbers), 6),
            "p75": round(_percentile(numbers, 0.75), 6),
            "max": round(max(numbers), 6),
        }
    return result


def _relaxable_gate(
    node: ast.expr,
    values_rows: list[dict],
    pass_rate: float,
    settings: dict,
) -> dict | None:
    comparison = _numeric_compare(node)
    if comparison is None:
        return None
    left, operator, original = comparison
    names = _gate_names(left)
    if not names or names & IMMUTABLE_GATE_FIELDS or original == 0:
        return None
    expression = ast.unparse(left)
    observed = []
    for values in values_rows:
        try:
            number = _finite(evaluate_expression(expression, values))
        except (ProgramValidationError, ArithmeticError, TypeError, ValueError):
            number = None
        if number is not None:
            observed.append(number)
    if not observed:
        return None
    if min(observed) == 0 and max(observed) == 0:
        return None
    cfg = settings.get("strategy_lab", {}).get("adaptive_relaxation", {})
    if isinstance(operator, (ast.Gt, ast.GtE)):
        adjusted = _percentile(observed, float(cfg.get("minimum_gate_percentile", 0.25)))
        if adjusted >= original:
            return None
        symbol = ">=" if isinstance(operator, ast.GtE) else ">"
    elif isinstance(operator, (ast.Lt, ast.LtE)):
        adjusted = _percentile(observed, float(cfg.get("maximum_gate_percentile", 0.75)))
        if adjusted <= original:
            return None
        if "spread_bps" in names:
            adjusted = min(adjusted, float(cfg.get("critical_max_spread_bps", 50.0)))
        symbol = "<=" if isinstance(operator, ast.LtE) else "<"
    else:
        return None
    if abs(adjusted - original) < 1e-9:
        return None
    return {
        "gate": ast.unparse(node),
        "names": sorted(names),
        "pass_rate": round(pass_rate, 6),
        "original_threshold": original,
        "relaxed_threshold": round(adjusted, 6),
        "replacement": f"{expression} {symbol} {adjusted:.8g}",
    }


def profile_observation_program(experiment: dict, frames: list[dict], settings: dict) -> dict:
    program, compile_diagnostic = compile_observation_program(
        experiment.get("strategy_logic") or experiment.get("program") or {}
    )
    if not program:
        status = "missing_feature" if compile_diagnostic.get("status") == "needs_feature_code" else "invalid_contract"
        return {"feasibility_status": status, "compile_diagnostic": compile_diagnostic}

    universe = program.get("universe") if isinstance(program.get("universe"), dict) else {}
    universe_frames = [frame for frame in frames if _universe_matches(frame, universe)]
    eligible_frames = [
        frame for frame in universe_frames
        if str(frame.get("session_status") or "").lower() not in {"closed", "stale", "unavailable"}
    ]
    if not universe_frames:
        return {
            "feasibility_status": "missing_surface_data",
            "observation_count": len(frames),
            "universe_match_count": 0,
            "candidate_count": 0,
            "entry_pass_rate": 0.0,
            "feature_profile": {},
            "gate_profile": {},
            "nearest_candidates": [],
            "relaxation": {},
            "program": program,
        }

    evaluated_frames: list[tuple[dict, dict]] = []
    runtime_errors = 0
    for frame in eligible_frames:
        try:
            evaluated_frames.append((frame, _program_values(frame, program)))
        except (ProgramValidationError, ArithmeticError, TypeError, ValueError, OverflowError):
            runtime_errors += 1
    values_rows = [values for _, values in evaluated_frames]
    gates = _top_level_gates(program["entry_expression"])
    gate_rows = []
    for gate in gates:
        passed = 0
        errors = 0
        text = ast.unparse(gate)
        for values in values_rows:
            try:
                passed += int(bool(evaluate_expression(text, values)))
            except (ProgramValidationError, ArithmeticError, TypeError, ValueError, OverflowError):
                errors += 1
        rate = passed / len(values_rows) if values_rows else 0.0
        gate_rows.append({"gate": text, "passed": passed, "errors": errors, "pass_rate": round(rate, 6)})

    candidates = 0
    nearest = []
    for frame, values in evaluated_frames:
        failed = []
        for gate in gates:
            try:
                if not bool(evaluate_expression(ast.unparse(gate), values)):
                    failed.append(ast.unparse(gate))
            except (ProgramValidationError, ArithmeticError, TypeError, ValueError, OverflowError):
                failed.append("expression_runtime_error")
        if not failed:
            candidates += 1
        elif len(failed) <= 3:
            nearest.append({
                "venue": frame.get("venue"),
                "inst_id": frame.get("inst_id"),
                "failed_gate_count": len(failed),
                "failed_gates": failed,
            })

    feature_profile = _feature_profile(values_rows)
    relaxations = []
    for gate, gate_profile in zip(gates, gate_rows):
        proposed = _relaxable_gate(gate, values_rows, float(gate_profile["pass_rate"]), settings)
        if proposed:
            relaxations.append(proposed)
    max_gates = int(settings.get("strategy_lab", {}).get("adaptive_relaxation", {}).get("max_gates_per_revision", 3))
    relaxations.sort(key=lambda item: (item["pass_rate"], item["gate"]))
    relaxations = relaxations[:max_gates]

    relaxation_gates = {item["gate"] for item in relaxations}
    blocking_gates = []
    for gate, gate_profile in zip(gates, gate_rows):
        if gate_profile["pass_rate"] > 0 or gate_profile["gate"] in relaxation_gates:
            continue
        names = _gate_names(gate)
        if names & IMMUTABLE_GATE_FIELDS:
            reason = "observation_safety_gate"
        elif any(
            name not in feature_profile or (
                feature_profile[name].get("min") == 0
                and feature_profile[name].get("max") == 0
            )
            for name in names
        ):
            reason = "missing_feature_history"
        else:
            reason = "unrelaxable_expression"
        blocking_gates.append({**gate_profile, "names": sorted(names), "reason": reason})
    if candidates:
        status = "feasible_active"
    elif any(item["reason"] == "observation_safety_gate" for item in blocking_gates):
        status = "blocked_observation_safety"
    elif any(item["reason"] == "missing_feature_history" for item in blocking_gates):
        status = "missing_feature_history"
    elif blocking_gates:
        status = "unrelaxable_contract"
    elif relaxations:
        status = "impossible_threshold"
    else:
        status = "feasible_rare"
    return {
        "feasibility_status": status,
        "observation_count": len(frames),
        "universe_match_count": len(universe_frames),
        "eligible_observation_count": len(eligible_frames),
        "candidate_count": candidates,
        "entry_pass_rate": round(candidates / len(values_rows), 6) if values_rows else 0.0,
        "feature_profile": feature_profile,
        "gate_profile": {item["gate"]: item for item in gate_rows},
        "blocking_gates": blocking_gates,
        "nearest_candidates": sorted(nearest, key=lambda item: item["failed_gate_count"])[:10],
        "relaxation": {
            "eligible": bool(relaxations) and not blocking_gates,
            "complete_repair": bool(relaxations) and not blocking_gates,
            "changes": relaxations,
        },
        "runtime_error_count": runtime_errors,
        "program": program,
    }


def record_contract_evaluation(
    conn: sqlite3.Connection,
    experiment: dict,
    profile: dict,
    *,
    cycle_id: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        insert into strategy_contract_evaluations(
            strategy_lab_id, strategy_lab_version, evaluated_at, feasibility_status,
            observation_count, universe_match_count, candidate_count, entry_pass_rate,
            feature_profile_json, gate_profile_json, nearest_candidates_json,
            relaxation_json, source_cycle_id
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            experiment["strategy_lab_id"], int(experiment.get("version") or 1), _utc(),
            profile.get("feasibility_status", "invalid_contract"),
            int(profile.get("observation_count") or 0), int(profile.get("universe_match_count") or 0),
            int(profile.get("candidate_count") or 0), float(profile.get("entry_pass_rate") or 0),
            json.dumps(profile.get("feature_profile") or {}, sort_keys=True),
            json.dumps(profile.get("gate_profile") or {}, sort_keys=True),
            json.dumps(profile.get("nearest_candidates") or [], sort_keys=True),
            json.dumps(profile.get("relaxation") or {}, sort_keys=True), cycle_id,
        ),
    )
    return int(cursor.lastrowid)


def maybe_create_relaxed_child(
    conn: sqlite3.Connection,
    experiment: dict,
    profile: dict,
    settings: dict,
) -> dict | None:
    cfg = settings.get("strategy_lab", {}).get("adaptive_relaxation", {})
    if not cfg.get("enabled", True) or profile.get("candidate_count"):
        return None
    max_depth = max(0, int(cfg.get("max_lineage_depth", 2)))
    depth = 0
    parent_id = experiment.get("parent_strategy_lab_id")
    seen = {str(experiment.get("strategy_lab_id") or "")}
    while parent_id and depth <= max_depth:
        parent_text = str(parent_id)
        if parent_text in seen:
            break
        seen.add(parent_text)
        depth += 1
        parent = conn.execute(
            "select parent_strategy_lab_id from strategy_lab_experiments where strategy_lab_id = ?",
            (parent_text,),
        ).fetchone()
        parent_id = parent["parent_strategy_lab_id"] if parent else None
    if depth >= max_depth:
        return {
            "status": "lineage_depth_reached",
            "strategy_lab_id": experiment.get("strategy_lab_id"),
            "lineage_depth": depth,
            "max_lineage_depth": max_depth,
        }
    relaxation = profile.get("relaxation") or {}
    changes = list(relaxation.get("changes") or [])
    if not changes or not relaxation.get("complete_repair"):
        return None
    aggregate = conn.execute(
        """
        select count(*) as scans, coalesce(sum(universe_match_count),0) as observations
        from strategy_contract_evaluations where strategy_lab_id=?
        """,
        (experiment["strategy_lab_id"],),
    ).fetchone()
    if int(aggregate["scans"] or 0) < int(cfg.get("min_eligible_scans", 6)) and int(
        aggregate["observations"] or 0
    ) < int(cfg.get("min_eligible_observations", 250)):
        return None
    existing = conn.execute(
        """
        select strategy_lab_id, status from strategy_lab_experiments
        where parent_strategy_lab_id=? and strategy_lab_id like ?
          and status not in ('retired_bad_evidence','retired_no_activity','rejected_invalid')
        order by created_at desc limit 1
        """,
        (experiment["strategy_lab_id"], f"{experiment['strategy_lab_id']}__relaxed_r%"),
    ).fetchone()
    if existing:
        return {"status": "existing", "strategy_lab_id": existing["strategy_lab_id"]}

    program = dict(profile["program"])
    clauses = [ast.unparse(node) for node in _top_level_gates(program["entry_expression"])]
    replacements = {item["gate"]: item["replacement"] for item in changes}
    program["entry_expression"] = " and ".join(replacements.get(clause, clause) for clause in clauses)
    revision = int(conn.execute(
        "select count(*) from strategy_lab_experiments where parent_strategy_lab_id=?",
        (experiment["strategy_lab_id"],),
    ).fetchone()[0]) + 1
    child_id = f"{experiment['strategy_lab_id']}__relaxed_r{revision}"
    now = _utc()
    allocation = float(cfg.get("paper_allocation_multiplier", 0.10))
    risk_gates = dict(experiment.get("risk_gates") or {})
    risk_gates.update({
        "paper_allocation_multiplier": allocation,
        "adaptive_relaxation": {"revision": revision, "changes": changes, "parent": experiment["strategy_lab_id"]},
    })
    evaluation = {
        "adaptive_relaxation": {
            "mode": "aggressive_discovery",
            "parent_strategy_lab_id": experiment["strategy_lab_id"],
            "revision": revision,
            "changes": changes,
            "paper_allocation_multiplier": allocation,
        }
    }
    conn.execute(
        """
        insert into strategy_lab_experiments(
            strategy_lab_id, version, parent_strategy_lab_id, experiment_type, status,
            hypothesis, strategy_logic_json, original_strategy_logic_json,
            compiled_strategy_logic_json, compile_status, compile_diagnostics_json,
            data_requirements_json, risk_gates_json, promotion_rules_json,
            source_agent, source_recommendation_id, created_at, updated_at,
            evaluation_json, source_surface, permitted_target_surfaces_json,
            surface_policy_json, novelty_status
        ) values(?,1,?,'market_strategy','active_testing',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            child_id, experiment["strategy_lab_id"],
            f"Adaptive paper discovery child of {experiment.get('hypothesis') or experiment['strategy_lab_id']}",
            json.dumps(program, sort_keys=True), json.dumps(program, sort_keys=True),
            json.dumps(program, sort_keys=True), "compiled",
            json.dumps({"reason": "adaptive_relaxation", "changes": changes}, sort_keys=True),
            json.dumps(experiment.get("data_requirements") or {}, sort_keys=True),
            json.dumps(risk_gates, sort_keys=True),
            json.dumps(experiment.get("promotion_rules") or {}, sort_keys=True),
            "strategy_feasibility_profiler", experiment.get("source_recommendation_id"),
            now, now, json.dumps(evaluation, sort_keys=True), experiment.get("source_surface"),
            json.dumps(experiment.get("permitted_target_surface") or [], sort_keys=True),
            json.dumps(experiment.get("surface_policy") or {}, sort_keys=True), "novel_child",
        ),
    )
    return {"status": "created", "strategy_lab_id": child_id, "changes": changes}
