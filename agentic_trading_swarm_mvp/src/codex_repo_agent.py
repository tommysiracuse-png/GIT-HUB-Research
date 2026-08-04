"""Repository-aware Codex CLI backend for autonomous code evolution.

The host owns an isolated Git worktree. Codex edits it directly and emits JSONL
events so interrupted implementations can resume in the same persisted thread.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Iterator


PINNED_CODEX_PACKAGE = "openai-codex==0.144.4"
PINNED_CODEX_NPM_FALLBACK = "@openai/codex@0.146.0"
DEFAULT_MODEL = "gpt-5.6-sol"
SECRET_ENV_NAMES = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def codex_repo_agent_config(settings: dict, runs_dir: pathlib.Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": True,
        "model": DEFAULT_MODEL,
        "reasoning_effort": "high",
        "timeout_seconds": 1800,
        "resume_cooldown_seconds": 300,
        "max_resumes_per_cycle": 1,
        "post_promotion_health_grace_seconds": 600,
        "post_promotion_health_loops": 3,
        "runtime_package": PINNED_CODEX_PACKAGE,
        "npm_runtime_package": PINNED_CODEX_NPM_FALLBACK,
        "npm_fallback_enabled": True,
        "auto_install_npm_fallback": False,
        "runtime_dir": str(runs_dir / "codex_runtime"),
        "codex_home": str(runs_dir / "codex_home"),
        "session_log_dir": str(runs_dir / "codex_sessions"),
        "lock_path": str(runs_dir / "codex_repo_agent.lock"),
        "lock_stale_seconds": 2400,
        "api_key_envs": ["CODEX_API_KEY", "OPENAI_API_KEY"],
        "cli_path": None,
        "node_path": None,
        "pnpm_path": None,
        "network_access": True,
    }
    return {**defaults, **(settings.get("codex_repo_agent") or {})}


def _existing_path(value: object) -> pathlib.Path | None:
    if not value:
        return None
    path = pathlib.Path(str(value)).expanduser()
    return path.resolve() if path.exists() else None


def _latest_glob(pattern: str) -> pathlib.Path | None:
    matches = [path for path in pathlib.Path.home().glob(pattern) if path.exists()]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _discover_node(cfg: dict[str, Any]) -> pathlib.Path | None:
    configured = _existing_path(cfg.get("node_path") or os.environ.get("CODEX_NODE_PATH"))
    if configured:
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    return pathlib.Path(found).resolve() if found else _latest_glob(
        ".cache/codex-runtimes/*/dependencies/node/bin/node.exe"
    )


def _discover_pnpm(cfg: dict[str, Any]) -> pathlib.Path | None:
    configured = _existing_path(cfg.get("pnpm_path") or os.environ.get("CODEX_PNPM_PATH"))
    if configured:
        return configured
    found = shutil.which("pnpm") or shutil.which("pnpm.cmd")
    return pathlib.Path(found).resolve() if found else _latest_glob(
        ".cache/codex-runtimes/*/dependencies/bin/fallback/pnpm.cmd"
    )


def _codex_script(runtime_dir: pathlib.Path) -> pathlib.Path:
    return runtime_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


def _bundled_codex_path() -> pathlib.Path | None:
    try:
        module = importlib.import_module("codex_cli_bin")
        path = pathlib.Path(str(module.bundled_codex_path())).expanduser()
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None
    return path.resolve() if path.exists() else None


def ensure_codex_runtime(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve or bootstrap the pinned official CLI without exposing an API key."""

    configured = _existing_path(cfg.get("cli_path") or os.environ.get("CODEX_CLI_PATH"))
    if configured:
        return {"available": True, "command_prefix": [str(configured)], "source": "configured"}
    bundled = _bundled_codex_path()
    if bundled:
        return {
            "available": True,
            "command_prefix": [str(bundled)],
            "source": "bundled_python_package",
            "runtime_package": str(cfg["runtime_package"]),
        }
    if not cfg.get("npm_fallback_enabled", True):
        return {"available": False, "reason": "bundled_codex_runtime_missing"}
    runtime_dir = pathlib.Path(str(cfg["runtime_dir"])).expanduser().resolve()
    script = _codex_script(runtime_dir)
    node = _discover_node(cfg)
    if script.exists() and node:
        return {
            "available": True,
            "command_prefix": [str(node), str(script)],
            "source": "pinned_runtime",
            "runtime_package": str(cfg["npm_runtime_package"]),
        }
    if not cfg.get("auto_install_npm_fallback", False):
        return {"available": False, "reason": "codex_runtime_missing", "runtime_dir": str(runtime_dir)}
    pnpm = _discover_pnpm(cfg)
    if not node or not pnpm:
        return {
            "available": False,
            "reason": "codex_runtime_bootstrap_tools_missing",
            "node_found": bool(node),
            "pnpm_found": bool(pnpm),
        }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    install_env = os.environ.copy()
    install_env["PATH"] = os.pathsep.join([str(node.parent), install_env.get("PATH", "")])
    try:
        completed = subprocess.run(
            [str(pnpm), "--dir", str(runtime_dir), "add", "--save-exact", str(cfg["npm_runtime_package"])],
            cwd=str(runtime_dir),
            env=install_env,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": "codex_runtime_install_failed", "error": str(exc)}
    if completed.returncode != 0 or not script.exists():
        return {
            "available": False,
            "reason": "codex_runtime_install_failed",
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-1000:],
            "stderr_tail": completed.stderr[-1000:],
        }
    return {
        "available": True,
        "command_prefix": [str(node), str(script)],
        "source": "pinned_runtime_bootstrap",
        "runtime_package": str(cfg["npm_runtime_package"]),
    }


def _api_key(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    for name in cfg.get("api_key_envs") or []:
        value = os.environ.get(str(name))
        if value:
            return str(name), value
    return None, None


def _child_environment(cfg: dict[str, Any], api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in SECRET_ENV_NAMES:
        env.pop(name, None)
    env["CODEX_API_KEY"] = api_key
    env["CODEX_HOME"] = str(pathlib.Path(str(cfg["codex_home"])).expanduser().resolve())
    env["CODEX_NON_INTERACTIVE"] = "1"
    return env


def build_implementation_prompt(
    payload: dict,
    preflight_hints: dict | None = None,
    failure_context: dict | None = None,
) -> str:
    code_change = payload.get("code_change") if isinstance(payload.get("code_change"), dict) else {}
    supplied_patch = (
        payload.get("unified_diff")
        or payload.get("patch")
        or payload.get("diff")
        or code_change.get("unified_diff")
        or code_change.get("patch")
        or code_change.get("diff")
        or ""
    )
    contract = {
        "title": payload.get("title"),
        "goal": payload.get("proposed_change") or payload.get("rationale"),
        "evidence": payload.get("evidence"),
        "expected_paper_only_impact": payload.get("expected_paper_only_impact"),
        "acceptance_criteria": payload.get("acceptance_criteria") or payload.get("tests_to_run"),
        "rollback_criteria": payload.get("rollback_criteria")
        or code_change.get("rollback_criteria"),
        "supplied_patch_hint": str(supplied_patch)[:24000],
    }
    hints = {
        "possible_files": (preflight_hints or {}).get("target_files") or [],
        "possible_tests": (preflight_hints or {}).get("parsed_tests") or [],
        "preflight_notes": (preflight_hints or {}).get("quality_scorecard") or {},
    }
    parts = [
        "You own this implementation inside an isolated Git worktree.",
        "Search the repository and read the exact current files before editing. Implement the goal end to end, edit incrementally, run focused tests, inspect failures, and repair your work. Do not merely describe a patch.",
        "The host independently runs focused tests and the full regression suite, commits, and promotes. Do not commit or change branches yourself.",
        "Supplied paths, tests, and patch text are non-authoritative hints. Correct them by searching the repository and choose any tracked files needed for a complete implementation.",
        "Never inspect, print, persist, transmit, or modify credentials or secret-bearing environment variables. Do not edit .git, runs, runtime databases, or local secret configuration.",
        "Keep live trading disabled. Do not make broker/order writes or nonzero real-money execution reachable from paper mode.",
        "You may change source, tests, configuration, dependency manifests, migrations, worker/startup code, and self-repair logic when the goal requires it.",
        "Finish with changed behavior and tests. If the turn must stop early, leave coherent work in place and state the exact next action.",
        "IMPLEMENTATION CONTRACT\n" + json.dumps(contract, sort_keys=True, default=str),
        "NON-AUTHORITATIVE PREFLIGHT HINTS\n" + json.dumps(hints, sort_keys=True, default=str),
    ]
    if failure_context:
        parts.extend(
            [
                "HOST VALIDATION OR PRIOR-TURN FAILURE CONTEXT\n"
                + json.dumps(failure_context, sort_keys=True, default=str)[-24000:],
                "Repair the existing work in place and rerun the relevant tests.",
            ]
        )
    return "\n\n".join(parts)


def _command(
    runtime: dict[str, Any], cfg: dict[str, Any], worktree_root: pathlib.Path, session_id: str | None
) -> list[str]:
    shared = [
        "--json",
        "--model",
        str(cfg["model"]),
        "--ignore-user-config",
        "-c",
        'approval_policy="never"',
        "-c",
        f'model_reasoning_effort="{cfg["reasoning_effort"]}"',
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "-c",
        'shell_environment_policy.exclude=["*KEY*","*SECRET*","*TOKEN*","*PASSWORD*","*CREDENTIAL*"]',
    ]
    prefix = list(runtime["command_prefix"])
    if session_id:
        return [*prefix, "exec", "resume", *shared, "--all", session_id, "-"]
    return [
        *prefix,
        "exec",
        *shared,
        "--color",
        "never",
        "--sandbox",
        "workspace-write",
        "-c",
        f"sandbox_workspace_write.network_access={'true' if cfg.get('network_access', True) else 'false'}",
        "--cd",
        str(worktree_root),
        "-",
    ]


def _parse_events(text: str) -> dict[str, Any]:
    session_id: str | None = None
    event_types: list[str] = []
    usage: dict[str, Any] = {}
    last_message = ""
    turn_completed = False
    turn_failed = False
    parse_errors = 0
    for raw in text.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            parse_errors += bool(raw.strip())
            continue
        event_type = str(event.get("type") or "")
        if event_type:
            event_types.append(event_type)
        if event_type == "thread.started":
            session_id = str(event.get("thread_id") or event.get("session_id") or "") or session_id
        elif event_type == "turn.completed":
            turn_completed = True
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else usage
        elif event_type in {"turn.failed", "error"}:
            turn_failed = True
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            last_message = str(item["text"])
    return {
        "session_id": session_id,
        "event_types": event_types[-100:],
        "usage": usage,
        "last_message": last_message[-6000:],
        "turn_completed": turn_completed,
        "turn_failed": turn_failed,
        "parse_errors": int(parse_errors),
    }


def _write_event_log(path: pathlib.Path, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(output)
        if output and not output.endswith("\n"):
            handle.write("\n")


def _scrub_secret_text(value: str, *secrets: str | None) -> str:
    scrubbed = value
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed


@contextlib.contextmanager
def codex_write_lock(cfg: dict[str, Any], owner: str) -> Iterator[dict[str, Any]]:
    path = pathlib.Path(str(cfg["lock_path"])).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_seconds = int(cfg.get("lock_stale_seconds", 2400))
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"owner": owner, "pid": os.getpid(), "acquired_at": _utc_now()}, handle)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0
            if age <= stale_seconds:
                yield {"acquired": False, "reason": "codex_writer_busy", "lock_path": str(path)}
                return
            try:
                path.unlink()
            except OSError:
                yield {"acquired": False, "reason": "stale_codex_writer_lock_unremovable", "lock_path": str(path)}
                return
    else:
        yield {"acquired": False, "reason": "codex_writer_busy", "lock_path": str(path)}
        return
    try:
        yield {"acquired": True, "lock_path": str(path)}
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def run_codex_repo_agent(
    *,
    proposal_id: str,
    payload: dict,
    preflight_hints: dict,
    worktree_root: pathlib.Path,
    settings: dict,
    runs_dir: pathlib.Path,
    session_id: str | None = None,
    failure_context: dict | None = None,
) -> dict[str, Any]:
    """Run or resume one repository-aware implementation turn."""

    cfg = codex_repo_agent_config(settings, runs_dir)
    if not cfg.get("enabled", True):
        return {"status": "unavailable", "reason": "codex_repo_agent_disabled"}
    runtime = ensure_codex_runtime(cfg)
    if not runtime.get("available"):
        return {"status": "unavailable", "reason": runtime.get("reason"), "runtime": runtime}
    source_env, key = _api_key(cfg)
    if not key:
        return {"status": "unavailable", "reason": "missing_codex_api_key", "runtime": runtime}

    pathlib.Path(str(cfg["codex_home"])).expanduser().mkdir(parents=True, exist_ok=True)
    log_path = pathlib.Path(str(cfg["session_log_dir"])) / f"{proposal_id.replace(':', '_')}.jsonl"
    command = _command(runtime, cfg, worktree_root.resolve(), session_id)
    prompt = build_implementation_prompt(payload, preflight_hints, failure_context)
    started_at = _utc_now()
    runtime_meta = {name: value for name, value in runtime.items() if name != "command_prefix"}
    try:
        completed = subprocess.run(
            command,
            cwd=str(worktree_root),
            env=_child_environment(cfg, key),
            input=prompt,
            text=True,
            capture_output=True,
            timeout=int(cfg.get("timeout_seconds", 1800)),
            check=False,
        )
        stdout = _scrub_secret_text(completed.stdout or "", key)
        stderr = _scrub_secret_text(completed.stderr or "", key)
        parsed = _parse_events(stdout)
        _write_event_log(log_path, stdout)
        status = "completed" if completed.returncode == 0 and parsed["turn_completed"] else "failed"
        if status == "failed" and (parsed.get("session_id") or session_id):
            status = "implementation_paused"
        return {
            "status": status,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "returncode": completed.returncode,
            "session_id": parsed.get("session_id") or session_id,
            "resumed": bool(session_id),
            "model": str(cfg["model"]),
            "reasoning_effort": str(cfg["reasoning_effort"]),
            "api_key_source": source_env,
            "runtime": runtime_meta,
            "event_log": str(log_path),
            "event_summary": parsed,
            "stderr_tail": stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        stdout = _scrub_secret_text(stdout, key)
        stderr = _scrub_secret_text(stderr, key)
        parsed = _parse_events(stdout)
        _write_event_log(log_path, stdout)
        return {
            "status": "implementation_paused",
            "reason": "codex_turn_timeout",
            "started_at": started_at,
            "finished_at": _utc_now(),
            "session_id": parsed.get("session_id") or session_id,
            "resumed": bool(session_id),
            "model": str(cfg["model"]),
            "reasoning_effort": str(cfg["reasoning_effort"]),
            "api_key_source": source_env,
            "runtime": runtime_meta,
            "event_log": str(log_path),
            "event_summary": parsed,
            "stderr_tail": stderr[-4000:],
        }
    except OSError as exc:
        return {"status": "unavailable", "reason": "codex_process_start_failed", "error": str(exc), "runtime": runtime_meta}
