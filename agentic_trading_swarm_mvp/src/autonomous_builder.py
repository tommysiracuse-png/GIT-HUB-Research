#!/usr/bin/env python3
"""Direct autonomous code builder for the paper-only radar.

This module intentionally bypasses the recommendation inbox/classifier chain:
it reads the current state packet and reports, asks a frontier model for one
concrete code patch, then sends that patch straight through the existing
sandbox/test/apply machinery. Hard safety boundaries remain deterministic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import uuid
from typing import Any

from code_evolution import process_code_change_recommendation, write_code_evolution_reports
from cost_router import ModelResult, complete, estimate_tokens, load_llm_config
from llm_bridge import STATE_JSON
from storage import RUNS_DIR, connect, record_llm_cost_event


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_JSON = RUNS_DIR / "autonomous_builder_report.json"
REPORT_MD = RUNS_DIR / "autonomous_builder_report.md"
MARKER = RUNS_DIR / "autonomous_builder_last_run.txt"
LOCK = RUNS_DIR / "autonomous_builder.lock"

REPORT_CONTEXT_FILES = [
    RUNS_DIR / "llm_state_packet.json",
    RUNS_DIR / "llm_swarm_latest.json",
    RUNS_DIR / "self_improvement_report.json",
    RUNS_DIR / "evolution_report.json",
    RUNS_DIR / "self_improvement_open_pack.json",
    RUNS_DIR / "frontier_crypto_venues_latest.json",
    RUNS_DIR / "prediction_markets_latest.json",
    RUNS_DIR / "improvement_backlog.md",
    RUNS_DIR / "growth_plan.md",
]

CODE_CONTEXT_FILES = [
    ROOT / "src" / "llm_swarm_runner.py",
    ROOT / "src" / "code_evolution.py",
    ROOT / "src" / "llm_bridge.py",
    ROOT / "src" / "self_improvement.py",
    ROOT / "src" / "radar_loop.py",
    ROOT / "config" / "settings.example.json",
]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _cfg(settings: dict) -> dict:
    defaults = {
        "enabled": True,
        "auto_run": True,
        "min_minutes_between_runs": 0,
        "lock_stale_minutes": 180,
        "model_tier": "standard",
        "plan_max_context_chars": 35000,
        "implementation_report_context_chars": 12000,
        "plan_timeout_seconds": 240,
        "implementation_timeout_seconds": 240,
        "plan_reasoning_effort": "medium",
        "implementation_reasoning_effort": "medium",
        "max_context_chars": 70000,
        "max_report_chars": 12000,
        "max_code_file_chars": 12000,
        "plan_max_output_tokens": 12000,
        "max_output_tokens": 12000,
        "implementation_strategy": "code_evolution_handoff",
        "require_unified_diff": True,
        "use_hard_model_timeout": True,
    }
    return {**defaults, **settings.get("autonomous_builder", {})}


def _parse_iso(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def should_auto_run(settings: dict, force: bool = False) -> bool:
    cfg = _cfg(settings)
    if force:
        return bool(cfg.get("enabled", True))
    if not cfg.get("enabled", True) or not cfg.get("auto_run", True):
        return False
    if _lock_active(settings):
        return False
    if not MARKER.exists():
        return True
    parsed = _parse_iso(MARKER.read_text(encoding="utf-8").strip())
    if not parsed:
        return True
    age_minutes = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 60.0
    return age_minutes >= float(cfg.get("min_minutes_between_runs", 60))


def _lock_active(settings: dict) -> bool:
    if not LOCK.exists():
        return False
    try:
        raw = LOCK.read_text(encoding="utf-8").strip()
        payload = json.loads(raw)
    except (OSError, TypeError, ValueError):
        # Legacy timestamp locks remain readable during rollout.
        try:
            parsed = _parse_iso(raw.splitlines()[0])
        except (NameError, IndexError):
            parsed = None
        if not parsed:
            return False
        age_minutes = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 60.0
        return age_minutes < float(_cfg(settings).get("lock_stale_minutes", 180))
    pid = int(payload.get("pid") or 0)
    if pid <= 0 or not _pid_alive(pid):
        return False
    expected_start = str(payload.get("process_start_time") or "")
    actual_start = _process_start_time(pid)
    if expected_start and actual_start and expected_start != actual_start:
        return False
    heartbeat = _parse_iso(str(payload.get("heartbeat_at") or payload.get("acquired_at") or ""))
    if not heartbeat:
        return False
    age_minutes = (dt.datetime.now(dt.timezone.utc) - heartbeat).total_seconds() / 60.0
    return age_minutes < float(_cfg(settings).get("lock_stale_minutes", 180))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, SystemError):
        return False


def _process_start_time(pid: int) -> str | None:
    try:
        import psutil  # type: ignore

        return f"{psutil.Process(pid).create_time():.6f}"
    except (ImportError, OSError, ValueError):
        if pid == os.getpid():
            return str(getattr(_process_start_time, "_self_token", None) or "") or None
        return None


_process_start_time._self_token = _utc_now()  # type: ignore[attr-defined]


def _acquire_lock(settings: dict) -> str | None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and not _lock_active(settings):
        try:
            LOCK.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    run_id = uuid.uuid4().hex
    now = _utc_now()
    payload = {
        "run_id": run_id,
        "pid": os.getpid(),
        "process_start_time": _process_start_time(os.getpid()),
        "acquired_at": now,
        "heartbeat_at": now,
        "repo_root": str(ROOT),
    }
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
    return run_id


def _refresh_lock(run_id: str) -> None:
    try:
        payload = json.loads(LOCK.read_text(encoding="utf-8"))
        if payload.get("run_id") != run_id:
            return
        payload["heartbeat_at"] = _utc_now()
        temporary = LOCK.with_suffix(".lock.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, LOCK)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return


def _release_lock(run_id: str | None = None) -> None:
    try:
        if run_id:
            payload = json.loads(LOCK.read_text(encoding="utf-8"))
            if payload.get("run_id") != run_id:
                return
        LOCK.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _read_text(path: pathlib.Path, max_chars: int) -> str:
    if not path.exists() or not path.is_file():
        return "<missing>"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<read failed: {exc}>"
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n<truncated>\n"


def _collect_context(settings: dict, *, include_code: bool = True, max_context_chars: int | None = None) -> str:
    cfg = _cfg(settings)
    report_chars = int(cfg.get("max_report_chars", 12000))
    code_chars = int(cfg.get("max_code_file_chars", 12000))
    sections: list[str] = []
    for path in REPORT_CONTEXT_FILES:
        sections.append(f"### REPORT {path.relative_to(ROOT)}\n{_read_text(path, report_chars)}")
    if include_code:
        for path in CODE_CONTEXT_FILES:
            sections.append(f"### CODE {path.relative_to(ROOT)}\n{_read_text(path, code_chars)}")
    context = "\n\n".join(sections)
    return context[: int(max_context_chars or cfg.get("max_context_chars", 70000))]


def _collect_implementation_context(settings: dict, plan: dict) -> str:
    cfg = _cfg(settings)
    sections = [
        _collect_context(
            settings,
            include_code=False,
            max_context_chars=int(cfg.get("implementation_report_context_chars", 12000)),
        )
    ]
    expected = plan.get("expected_files") if isinstance(plan.get("expected_files"), list) else []
    files = [ROOT / str(path) for path in expected if str(path).startswith(("src/", "tests/", "config/"))]
    if not files:
        files = CODE_CONTEXT_FILES
    for path in files[:8]:
        try:
            label = path.relative_to(ROOT)
        except ValueError:
            continue
        sections.append(f"### CODE {label}\n{_read_text(path, int(cfg.get('max_code_file_chars', 12000)))}")
    return "\n\n".join(sections)[: int(cfg.get("max_context_chars", 70000))]


def _strip_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _parse_builder_json(text: str) -> dict | None:
    cleaned = _strip_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _build_plan_prompt(context: str) -> str:
    return (
        "You are the outside-flow autonomous builder for a paper-only market radar. "
        "You read the current state packet, latest swarm output, self-improvement outputs, "
        "evolution failures, and current code context. First create a concrete implementation plan. "
        "Do not write code yet.\n\n"
        "Authority: you may change paper-only runtime code, reports, tests, market-data coverage, "
        "quality scoring, LLM packet wiring, recommendation quality controls, or code-evolution "
        "repair logic. Prefer changes that make the system actually grow or self-repair, not report-only "
        "helpers nobody calls. Paper exploration is enabled: preserve priceable candidate emission and "
        "turn weak performance, route, quality, spread, liquidity, or cost evidence into diagnostics, "
        "ranking, sizing, synthetic-paper routing, or counterfactual guard-value measurement rather than "
        "new hard quarantines or paper-entry blocks.\n\n"
        "Hard blocks: do not enable live trading, credentials, broker/order writes, account capability "
        "changes, startup/system tasks, destructive data actions, real notional increases, or raw installer "
        "commands in runtime code. You may add Python dependencies by editing requirements-autonomous.txt "
        "or requirements-llm.txt; the evolution runner will install those manifests after sandbox tests pass.\n\n"
        "Return exactly one JSON object with this shape:\n"
        "{"
        "\"plan\": \"short implementation plan\", "
        "\"title\": \"short title\", "
        "\"priority\": 50-100, "
        "\"expected_behavior_change\": \"what changes in the running paper system\", "
        "\"paper_testable_surface\": \"one exact paper surface, not a broad strategy label\", "
        "\"behavioral_gate\": \"one observable paper-mode allow/reject or scoring condition\", "
        "\"evidence\": {\"source\": \"state/report evidence\"}, "
        "\"route_or_quality_evidence\": {\"route_evidence|quality_evidence\": \"specific supporting record\"}, "
        "\"implementation_notes\": [\"ordered concrete implementation steps\"], "
        "\"expected_files\": [\"repo-relative paths\"], "
        "\"tests_to_run\": [\"python -m unittest ...\"], "
        "\"change_category\": \"runtime_pipeline_integration|public_data_adapter|parser_improvement|scanner_expansion|paper_signal_variant|paper_scoring_logic|self_improvement_policy|evolution_loop_improvement|report_dashboard|llm_prompt_state_packet|quality_scoring|read_only_route_intelligence|tests_fixtures|dependency_management\", "
        "\"implementation_mode\": \"runtime_active|paper_policy|shadow_trial|report_only\", "
        "\"rollback_criteria\": \"specific rollback condition\", "
        "\"frontier_escalation_reason\": \"why this needs the outside builder\""
        "}\n\n"
        f"CURRENT CONTEXT:\n{context}"
    )


def _build_implementation_prompt(plan: dict, context: str) -> str:
    return (
        "You are the outside-flow autonomous builder for a paper-only market radar. "
        "Implement the approved plan below as one complete unified diff. "
        "You must make the code change yourself; do not return only a plan.\n\n"
        "Authority: you may change paper-only runtime code, reports, tests, market-data coverage, "
        "quality scoring, LLM packet wiring, recommendation quality controls, code-evolution repair logic, "
        "and repo-declared Python dependencies. Paper exploration is enabled: preserve priceable candidate "
        "emission and do not implement weak performance, route, quality, spread, liquidity, or cost evidence "
        "as a new hard quarantine or paper-entry block. Use diagnostics, ranking, sizing, synthetic-paper "
        "routing, or counterfactual guard-value measurement instead.\n\n"
        "Hard blocks: do not enable live trading, credentials, broker/order writes, account capability "
        "changes, startup/system tasks, destructive data actions, real notional increases, or raw installer "
        "commands in runtime code. Python dependencies may be declared only in requirements-autonomous.txt "
        "or requirements-llm.txt.\n\n"
        "Return only a complete valid git-apply-compatible unified diff. Do not return JSON, markdown, "
        "explanations, or code fences. Use exact current file context; include tests where practical; "
        "keep the patch narrow enough to pass.\n\n"
        f"APPROVED PLAN:\n{json.dumps(plan, sort_keys=True)}\n\n"
        f"CURRENT CONTEXT:\n{context}"
    )


def _write_report(report: dict) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Autonomous Builder Report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Model status: `{report.get('model', {}).get('status')}`",
        f"- Title: {report.get('title') or ''}",
        f"- Plan: {report.get('plan') or ''}",
    ]
    if report.get("created"):
        lines.extend(["", "## Created Artifacts", ""])
        for item in report["created"]:
            lines.append(
                f"- `{item.get('artifact_type')}` `{item.get('proposal_id')}` status=`{item.get('status')}`"
            )
    if report.get("reason"):
        lines.extend(["", "## Reason", "", str(report["reason"])])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _tier_model_result(
    *,
    agent_name: str,
    tier_name: str,
    operation: str,
    status: str,
    prompt: str,
    text: str,
    reasoning_effort: str | None,
    structured_json: bool,
    frontier_escalation_reason: str | None,
) -> ModelResult:
    cfg = load_llm_config()
    tier_cfg = cfg.get("tiers", {}).get(tier_name, cfg.get("tiers", {}).get("fast", {}))
    model_name = str(tier_cfg.get("model", "fallback"))
    result = ModelResult(
        text=text,
        model_name=model_name,
        model_tier=tier_name,
        prompt_tokens=estimate_tokens(prompt),
        completion_tokens=estimate_tokens(text),
        estimated_cost_usd=0.0,
        status=status,
        provider=model_name.split("/", 1)[0] if "/" in model_name else "openai",
        api=str(tier_cfg.get("api") or "responses"),
        reasoning_effort=reasoning_effort,
        reasoning_mode=tier_cfg.get("reasoning_mode"),
        verbosity=tier_cfg.get("verbosity"),
        operation=operation,
        prompt_cache_key=tier_cfg.get("prompt_cache_key") or f"radar:{agent_name}:{tier_name}",
        frontier_escalation_reason=frontier_escalation_reason,
        structured_json=structured_json,
    )
    with connect() as conn:
        record_llm_cost_event(
            conn,
            agent_name,
            result.model_tier,
            result.model_name,
            result.prompt_tokens,
            result.completion_tokens,
            result.estimated_cost_usd,
            result.status,
            provider=result.provider,
            api=result.api,
            reasoning_effort=result.reasoning_effort,
            verbosity=result.verbosity,
            operation=result.operation,
            prompt_cache_key=result.prompt_cache_key,
            frontier_escalation_reason=result.frontier_escalation_reason,
            structured_json=result.structured_json,
        )
    return result


def _model_result_from_dict(data: dict) -> ModelResult:
    return ModelResult(
        text=str(data.get("text") or ""),
        model_name=str(data.get("model_name") or "fallback"),
        model_tier=str(data.get("model_tier") or "fast"),
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        estimated_cost_usd=float(data.get("estimated_cost_usd") or 0.0),
        status=str(data.get("status") or "fallback_error:missing_status"),
        provider=str(data.get("provider") or ""),
        api=str(data.get("api") or ""),
        reasoning_effort=data.get("reasoning_effort"),
        reasoning_mode=data.get("reasoning_mode"),
        verbosity=data.get("verbosity"),
        operation=data.get("operation"),
        prompt_cache_key=data.get("prompt_cache_key"),
        frontier_escalation_reason=data.get("frontier_escalation_reason"),
        structured_json=bool(data.get("structured_json")),
    )


def _complete_with_hard_timeout(
    agent_name: str,
    prompt: str,
    *,
    system: str,
    tier_override: str,
    operation: str,
    frontier_escalation_reason: str,
    reasoning_effort_override: str,
    structured_json: bool,
    max_output_tokens_override: int,
    timeout_seconds_override: float,
    use_hard_timeout: bool,
) -> ModelResult:
    if not use_hard_timeout:
        return complete(
            agent_name,
            prompt,
            system=system,
            tier_override=tier_override,
            operation=operation,
            frontier_escalation_reason=frontier_escalation_reason,
            reasoning_effort_override=reasoning_effort_override,
            structured_json=structured_json,
            max_output_tokens_override=max_output_tokens_override,
            timeout_seconds_override=timeout_seconds_override,
        )

    payload = {
        "agent_name": agent_name,
        "prompt": prompt,
        "system": system,
        "tier_override": tier_override,
        "operation": operation,
        "frontier_escalation_reason": frontier_escalation_reason,
        "reasoning_effort_override": reasoning_effort_override,
        "structured_json": structured_json,
        "max_output_tokens_override": max_output_tokens_override,
        "timeout_seconds_override": timeout_seconds_override,
        "root": str(ROOT),
    }
    helper = r"""
import json
import pathlib
import sys

payload = json.loads(sys.stdin.read())
root = pathlib.Path(payload.pop("root"))
src = root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
from cost_router import complete  # noqa: E402

result = complete(**payload)
print(json.dumps({
    "text": result.text,
    "model_name": result.model_name,
    "model_tier": result.model_tier,
    "prompt_tokens": result.prompt_tokens,
    "completion_tokens": result.completion_tokens,
    "estimated_cost_usd": result.estimated_cost_usd,
    "status": result.status,
    "provider": result.provider,
    "api": result.api,
    "reasoning_effort": result.reasoning_effort,
    "reasoning_mode": result.reasoning_mode,
    "verbosity": result.verbosity,
    "operation": result.operation,
    "prompt_cache_key": result.prompt_cache_key,
    "frontier_escalation_reason": result.frontier_escalation_reason,
    "structured_json": result.structured_json,
}, sort_keys=True))
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", helper],
            input=json.dumps(payload),
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=max(1.0, float(timeout_seconds_override) + 15.0),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _tier_model_result(
            agent_name=agent_name,
            tier_name=tier_override,
            operation=operation,
            status=f"fallback_error:hard_timeout_after_{float(timeout_seconds_override):.0f}s",
            prompt=prompt,
            text="",
            reasoning_effort=reasoning_effort_override,
            structured_json=structured_json,
            frontier_escalation_reason=frontier_escalation_reason,
        )

    if proc.returncode != 0:
        return _tier_model_result(
            agent_name=agent_name,
            tier_name=tier_override,
            operation=operation,
            status=f"fallback_error:model_subprocess_exit_{proc.returncode}",
            prompt=prompt,
            text=(proc.stderr or proc.stdout or "")[:2000],
            reasoning_effort=reasoning_effort_override,
            structured_json=structured_json,
            frontier_escalation_reason=frontier_escalation_reason,
        )
    try:
        return _model_result_from_dict(json.loads(proc.stdout.strip().splitlines()[-1]))
    except (IndexError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _tier_model_result(
            agent_name=agent_name,
            tier_name=tier_override,
            operation=operation,
            status=f"fallback_error:model_subprocess_bad_json:{exc}",
            prompt=prompt,
            text=(proc.stdout or proc.stderr or "")[:2000],
            reasoning_effort=reasoning_effort_override,
            structured_json=structured_json,
            frontier_escalation_reason=frontier_escalation_reason,
        )


def _mark_run() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(_utc_now(), encoding="utf-8")


def _proposal_id_seed(parsed: dict, text: str) -> str:
    seed = json.dumps(parsed, sort_keys=True) + text[:1000]
    return "autonomous_builder:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _run_with_conn(conn: Any, settings: dict, *, force: bool = False) -> dict:
    if _lock_active(settings):
        return _write_report({"generated_at": _utc_now(), "status": "already_running"})
    if not should_auto_run(settings, force=force):
        return _write_report({"generated_at": _utc_now(), "status": "not_due"})
    run_id = _acquire_lock(settings)
    if not run_id:
        return _write_report({"generated_at": _utc_now(), "status": "already_running"})
    try:
        return _run_with_conn_locked(conn, settings, lock_run_id=run_id)
    finally:
        _release_lock(run_id)


def _run_with_conn_locked(conn: Any, settings: dict, *, lock_run_id: str | None = None) -> dict:
    cfg = _cfg(settings)
    plan_context = _collect_context(
        settings,
        include_code=False,
        max_context_chars=int(cfg.get("plan_max_context_chars", 35000)),
    )
    if lock_run_id:
        _refresh_lock(lock_run_id)
    plan_result = _complete_with_hard_timeout(
        "autonomous_builder",
        _build_plan_prompt(plan_context),
        system="You are an autonomous paper-only code planner. Return JSON only.",
        tier_override=str(cfg.get("model_tier", "frontier")),
        operation="autonomous_builder_plan",
        frontier_escalation_reason="Direct outside-flow autonomous code evolution.",
        reasoning_effort_override=str(cfg.get("plan_reasoning_effort", "max")),
        structured_json=True,
        max_output_tokens_override=int(cfg.get("plan_max_output_tokens", 24000)),
        timeout_seconds_override=float(cfg.get("plan_timeout_seconds", 420)),
        use_hard_timeout=bool(cfg.get("use_hard_model_timeout", True)),
    )
    plan_model = {
        "name": plan_result.model_name,
        "tier": plan_result.model_tier,
        "status": plan_result.status,
        "estimated_cost_usd": plan_result.estimated_cost_usd,
        "prompt_tokens": plan_result.prompt_tokens,
        "completion_tokens": plan_result.completion_tokens,
        "reasoning_effort": plan_result.reasoning_effort,
        "reasoning_mode": plan_result.reasoning_mode,
    }
    if lock_run_id:
        _refresh_lock(lock_run_id)
    _mark_run()
    if not plan_result.status.startswith("model_call:"):
        return _write_report(
            {
                "generated_at": _utc_now(),
                "status": "model_unavailable",
                "model": plan_model,
                "plan_model": plan_model,
                "reason": "No patch attempted because the planning model call did not return a real model response.",
            }
        )

    plan = _parse_builder_json(plan_result.text)
    if not plan:
        return _write_report(
            {
                "generated_at": _utc_now(),
                "status": "invalid_plan_json",
                "model": plan_model,
                "plan_model": plan_model,
                "reason": plan_result.text[:2000],
            }
        )

    strategy = str(cfg.get("implementation_strategy", "code_evolution_handoff")).strip().lower()
    if strategy in {"code_evolution_handoff", "handoff", "plan_handoff", "plan_only"}:
        code_change = {
            "change_category": plan.get("change_category"),
            "implementation_mode": plan.get("implementation_mode"),
            "expected_files": plan.get("expected_files") or [],
            "tests_to_run": plan.get("tests_to_run") or [],
            "rollback_criteria": plan.get("rollback_criteria")
            or "Revert if tests fail or paper-only safety checks fail.",
            "paper_testable_surface": plan.get("paper_testable_surface"),
            "behavioral_gate": plan.get("behavioral_gate"),
            "frontier_escalation_reason": plan.get("frontier_escalation_reason")
            or "Direct outside-flow autonomous code-evolution handoff.",
            "evidence": {
                **(plan.get("evidence") if isinstance(plan.get("evidence"), dict) else {"summary": str(plan.get("evidence") or ""), "source": "autonomous_builder_plan"}),
                **(plan.get("route_or_quality_evidence") if isinstance(plan.get("route_or_quality_evidence"), dict) else {}),
            },
        }
        payload = {
            "action": "propose_code_change",
            "agent_name": "autonomous_builder",
            "title": plan.get("title") or "Autonomous builder handoff",
            "priority": int(plan.get("priority") or 95),
            "rationale": plan.get("expected_behavior_change") or plan.get("plan") or "Direct autonomous builder plan.",
            "proposed_change": plan.get("plan") or plan.get("expected_behavior_change") or "",
            "evidence": code_change["evidence"],
            "frontier_escalation_reason": code_change["frontier_escalation_reason"],
            "model": plan_model,
            "plan_model": plan_model,
            "autonomous_plan": plan,
            "autonomous_builder_strategy": strategy,
            "code_change": code_change,
        }
        rec = {
            "recommendation_id": _proposal_id_seed({"plan": plan, "strategy": strategy}, plan_result.text),
            "title": payload["title"],
            "priority": payload["priority"],
            "payload": payload,
        }
        created = process_code_change_recommendation(conn, rec, settings)
        if lock_run_id:
            _refresh_lock(lock_run_id)
        write_code_evolution_reports(conn, settings)
        return _write_report(
            {
                "generated_at": _utc_now(),
                "status": "handed_off_to_code_evolution",
                "model": plan_model,
                "plan_model": plan_model,
                "title": payload["title"],
                "plan": plan,
                "created": created,
            }
        )

    context = _collect_implementation_context(settings, plan)
    impl_result = _complete_with_hard_timeout(
        "autonomous_builder",
        _build_implementation_prompt(plan, context),
        system="You are an autonomous paper-only code implementer. Return only a unified diff.",
        tier_override=str(cfg.get("model_tier", "frontier")),
        operation="autonomous_builder_implementation",
        frontier_escalation_reason=str(plan.get("frontier_escalation_reason") or "Direct outside-flow autonomous code implementation."),
        reasoning_effort_override=str(cfg.get("implementation_reasoning_effort", "high")),
        structured_json=False,
        max_output_tokens_override=int(cfg.get("max_output_tokens", 24000)),
        timeout_seconds_override=float(cfg.get("implementation_timeout_seconds", 420)),
        use_hard_timeout=bool(cfg.get("use_hard_model_timeout", True)),
    )
    model = {
        "name": impl_result.model_name,
        "tier": impl_result.model_tier,
        "status": impl_result.status,
        "estimated_cost_usd": impl_result.estimated_cost_usd,
        "prompt_tokens": impl_result.prompt_tokens,
        "completion_tokens": impl_result.completion_tokens,
        "reasoning_effort": impl_result.reasoning_effort,
        "reasoning_mode": impl_result.reasoning_mode,
    }
    if not impl_result.status.startswith("model_call:"):
        return _write_report(
            {
                "generated_at": _utc_now(),
                "status": "implementation_model_unavailable",
                "model": model,
                "plan_model": plan_model,
                "plan": plan,
                "reason": "No patch attempted because the implementation model call did not return a real model response.",
            }
        )

    diff = _strip_fence(impl_result.text)
    code_change = {
        "change_category": plan.get("change_category"),
        "implementation_mode": plan.get("implementation_mode"),
        "expected_files": plan.get("expected_files") or [],
        "tests_to_run": plan.get("tests_to_run") or [],
        "rollback_criteria": plan.get("rollback_criteria") or "Revert if tests fail or paper-only safety checks fail.",
        "paper_testable_surface": plan.get("paper_testable_surface"),
        "behavioral_gate": plan.get("behavioral_gate"),
        "evidence": {
            **(plan.get("evidence") if isinstance(plan.get("evidence"), dict) else {"source": "autonomous_builder"}),
            **(plan.get("route_or_quality_evidence") if isinstance(plan.get("route_or_quality_evidence"), dict) else {}),
        },
        "frontier_escalation_reason": plan.get("frontier_escalation_reason")
        or "Direct outside-flow autonomous code implementation.",
        "unified_diff": diff,
    }
    if cfg.get("require_unified_diff", True) and not diff:
        return _write_report(
            {
                "generated_at": _utc_now(),
                "status": "no_code_patch_returned",
                "model": model,
                "plan_model": plan_model,
                "title": plan.get("title"),
                "plan": plan,
                "reason": "Builder implementation response did not include a unified diff.",
            }
        )

    payload = {
        "action": "propose_code_change",
        "agent_name": "autonomous_builder",
        "title": plan.get("title") or "Autonomous builder patch",
        "priority": int(plan.get("priority") or 95),
        "rationale": plan.get("expected_behavior_change") or plan.get("plan") or "Direct autonomous builder patch.",
        "proposed_change": plan.get("plan") or plan.get("expected_behavior_change") or "",
        "evidence": code_change["evidence"],
        "frontier_escalation_reason": code_change["frontier_escalation_reason"],
        "model": model,
        "plan_model": plan_model,
        "autonomous_plan": plan,
        "code_change": code_change,
    }
    if diff:
        payload["unified_diff"] = diff
    rec = {
        "recommendation_id": _proposal_id_seed({"plan": plan, "diff": diff[:2000]}, impl_result.text),
        "title": payload["title"],
        "priority": payload["priority"],
        "payload": payload,
    }
    created = process_code_change_recommendation(conn, rec, settings)
    write_code_evolution_reports(conn, settings)
    return _write_report(
        {
            "generated_at": _utc_now(),
            "status": "attempted_patch",
            "model": model,
            "plan_model": plan_model,
            "title": payload["title"],
            "plan": plan,
            "created": created,
        }
    )


def run_autonomous_builder(settings: dict, *, conn: Any | None = None, force: bool = False) -> dict:
    if conn is not None:
        return _run_with_conn(conn, settings, force=force)
    with connect() as owned_conn:
        return _run_with_conn(owned_conn, settings, force=force)


if __name__ == "__main__":
    from settings import load_settings

    print(json.dumps(run_autonomous_builder(load_settings(), force=True), indent=2))
