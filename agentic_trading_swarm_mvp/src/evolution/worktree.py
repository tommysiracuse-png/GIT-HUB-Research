"""Local Git worktree release operations for code evolution."""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any

from .contracts import CandidateRelease


RUNTIME_DIR_NAMES = {"runs", ".venv", "__pycache__", ".pytest_cache"}
RUNTIME_FILE_SUFFIXES = {".pyc", ".log", ".sqlite", ".db"}


def run_git(args: list[str], cwd: pathlib.Path, timeout: int = 120) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        return {
            "args": ["git", *args],
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
    except Exception as exc:  # noqa: BLE001
        return {"args": ["git", *args], "returncode": 999, "stdout": "", "stderr": str(exc), "stdout_tail": "", "stderr_tail": str(exc)}


def repo_root(path: pathlib.Path) -> pathlib.Path | None:
    result = run_git(["rev-parse", "--show-toplevel"], path)
    if result["returncode"] != 0:
        return None
    return pathlib.Path(result["stdout"].strip()).resolve()


def current_commit(root: pathlib.Path) -> str | None:
    result = run_git(["rev-parse", "HEAD"], root)
    if result["returncode"] != 0:
        return None
    return result["stdout"].strip()


def changed_paths_between_commits(
    root: pathlib.Path,
    base_commit: str,
    target_commit: str,
    *,
    timeout: int = 120,
) -> tuple[set[str] | None, dict[str, Any]]:
    """Return tracked paths changed from ``base_commit`` to ``target_commit``."""

    diff = run_git(["diff", "--name-only", f"{base_commit}..{target_commit}"], root, timeout=timeout)
    if diff["returncode"] != 0:
        return None, {"ok": False, "reason": "changed_paths_failed", "command": diff}
    paths = {
        line.strip().replace("\\", "/")
        for line in diff["stdout"].splitlines()
        if line.strip()
    }
    return paths, {
        "ok": True,
        "base_commit": base_commit,
        "target_commit": target_commit,
        "command": diff,
    }


def update_champion_latest(root: pathlib.Path, commit: str | None = None, *, timeout: int = 120) -> dict[str, Any]:
    """Point ``champion/latest`` at an already-promoted commit."""

    target_commit = commit or current_commit(root)
    if not target_commit:
        return {"ok": False, "reason": "promoted_head_missing"}
    tag_name = "champion/latest"
    tag = run_git(["tag", "-f", tag_name, target_commit], root, timeout=timeout)
    if tag["returncode"] != 0:
        return {"ok": False, "reason": "champion_tag_failed", "tag": tag}
    return {"ok": True, "champion_tag": tag_name, "champion_commit": target_commit, "tag": tag}


def latest_champion_tag(root: pathlib.Path) -> str | None:
    result = run_git(["tag", "--list", "champion/*", "--sort=-creatordate"], root)
    if result["returncode"] != 0:
        return None
    tags = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    return tags[0] if tags else None


def dirty_source_paths(root: pathlib.Path, app_root: pathlib.Path) -> list[str]:
    result = run_git(["status", "--porcelain"], root)
    if result["returncode"] != 0:
        return ["<git-status-failed>"]
    app_rel = app_root.resolve().relative_to(root.resolve()).as_posix()
    dirty: list[str] = []
    for line in result["stdout"].splitlines():
        if not line:
            continue
        path = line[3:].replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        parts = pathlib.PurePosixPath(path).parts
        if any(part in RUNTIME_DIR_NAMES for part in parts):
            continue
        if pathlib.PurePosixPath(path).suffix in RUNTIME_FILE_SUFFIXES:
            continue
        if path.startswith(f"{app_rel}/src/") or path.startswith(f"{app_rel}/tests/") or path.startswith(f"{app_rel}/config/") or path.startswith(f"{app_rel}/scripts/"):
            dirty.append(path)
    return dirty


def release_preflight(app_root: pathlib.Path, *, require_clean: bool = True, require_champion: bool = True) -> dict[str, Any]:
    root = repo_root(app_root)
    if root is None:
        return {"ok": False, "reason": "ambiguous_repo_root", "repo_root": None}
    parent_commit = current_commit(root)
    if not parent_commit:
        return {"ok": False, "reason": "missing_parent_commit", "repo_root": str(root)}
    champion = latest_champion_tag(root)
    if require_champion and not champion:
        return {"ok": False, "reason": "missing_champion_tag", "repo_root": str(root), "parent_commit": parent_commit}
    dirty = dirty_source_paths(root, app_root) if require_clean else []
    if dirty:
        return {
            "ok": False,
            "reason": "dirty_source_tree",
            "repo_root": str(root),
            "parent_commit": parent_commit,
            "champion_tag": champion,
            "dirty_paths": dirty[:50],
        }
    return {
        "ok": True,
        "repo_root": str(root),
        "app_relative_root": app_root.resolve().relative_to(root.resolve()).as_posix(),
        "parent_commit": parent_commit,
        "champion_tag": champion,
    }


def _safe_branch_suffix(proposal_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9._-]+", "-", proposal_id.split(":", 1)[-1]).strip("-")
    return suffix[:60] or "candidate"


def _remove_stale_worktree(root: pathlib.Path, worktree: pathlib.Path, *, timeout: int) -> dict[str, Any]:
    remove = run_git(["worktree", "remove", "--force", str(worktree)], root, timeout=timeout)
    prune = run_git(["worktree", "prune"], root, timeout=timeout)
    errors: list[str] = []
    for delay in (0.0, 0.1, 0.25, 0.5):
        if not worktree.exists():
            break
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(worktree)
        except OSError as exc:
            errors.append(str(exc))
    return {
        "ok": not worktree.exists(),
        "remove": remove,
        "prune": prune,
        "filesystem_errors": errors[-4:],
    }


def _available_candidate_location(root: pathlib.Path, base_dir: pathlib.Path, suffix: str, *, timeout: int) -> tuple[str, pathlib.Path, dict[str, Any]]:
    branch = f"evolution/{suffix}"
    worktree = base_dir / suffix
    cleanup: dict[str, Any] = {"ok": True, "required": False}
    if worktree.exists():
        cleanup = {"required": True, **_remove_stale_worktree(root, worktree, timeout=timeout)}

    branch_check = run_git(["rev-parse", "--verify", branch], root, timeout=timeout)
    branch_deleted = None
    if branch_check["returncode"] == 0:
        branch_deleted = run_git(["branch", "-D", branch], root, timeout=timeout)

    if not worktree.exists() and (branch_deleted is None or branch_deleted["returncode"] == 0):
        return branch, worktree, {**cleanup, "fallback_used": False, "branch_delete": branch_deleted}

    # A Windows process can briefly retain a test or bytecode handle after a
    # candidate exits. Do not let that stale path abort the evolution cycle.
    for attempt in range(1, 100):
        candidate_branch = f"{branch}-retry-{attempt}"
        candidate_worktree = base_dir / f"{suffix}-retry-{attempt}"
        candidate_check = run_git(["rev-parse", "--verify", candidate_branch], root, timeout=timeout)
        if candidate_check["returncode"] != 0 and not candidate_worktree.exists():
            return candidate_branch, candidate_worktree, {
                **cleanup,
                "fallback_used": True,
                "branch_delete": branch_deleted,
                "fallback_attempt": attempt,
            }
    return branch, worktree, {**cleanup, "fallback_used": False, "exhausted": True, "branch_delete": branch_deleted}


def create_candidate_worktree(app_root: pathlib.Path, proposal_id: str, *, base_dir: pathlib.Path, timeout: int = 120) -> tuple[CandidateRelease | None, dict[str, Any]]:
    preflight = release_preflight(app_root)
    if not preflight.get("ok"):
        return None, preflight
    root = pathlib.Path(preflight["repo_root"])
    app_relative = preflight["app_relative_root"]
    suffix = _safe_branch_suffix(proposal_id)
    branch, worktree, cleanup = _available_candidate_location(root, base_dir, suffix, timeout=timeout)
    if cleanup.get("exhausted"):
        return None, {**preflight, "ok": False, "reason": "worktree_location_unavailable", "cleanup": cleanup}
    add = run_git(["worktree", "add", "-b", branch, str(worktree), preflight["parent_commit"]], root, timeout=timeout)
    if add["returncode"] != 0:
        return None, {**preflight, "ok": False, "reason": "worktree_create_failed", "command": add, "cleanup": cleanup}
    release = CandidateRelease(
        proposal_id=proposal_id,
        parent_commit=preflight["parent_commit"],
        branch_name=branch,
        worktree_path=str(worktree),
        app_worktree_path=str(worktree / app_relative),
    )
    return release, {**preflight, "ok": True, "branch_name": branch, "worktree_path": str(worktree), "cleanup": cleanup}


def commit_candidate(release: CandidateRelease, message: str, *, timeout: int = 120) -> tuple[CandidateRelease, dict[str, Any]]:
    worktree = pathlib.Path(release.worktree_path)
    add = run_git(["add", "--all"], worktree, timeout=timeout)
    if add["returncode"] != 0:
        return release, {"ok": False, "reason": "git_add_failed", "command": add}
    diff = run_git(["diff", "--cached", "--quiet"], worktree, timeout=timeout)
    if diff["returncode"] == 0:
        return release, {"ok": False, "reason": "no_changed_files"}
    commit = run_git(["commit", "-m", message], worktree, timeout=timeout)
    if commit["returncode"] != 0:
        return release, {"ok": False, "reason": "git_commit_failed", "command": commit}
    candidate = current_commit(worktree)
    release.candidate_commit = candidate
    release.status = "candidate_committed"
    return release, {"ok": True, "candidate_commit": candidate, "commit": commit}


def promote_candidate(
    release: CandidateRelease,
    app_root: pathlib.Path,
    *,
    timeout: int = 120,
    update_champion: bool = True,
) -> tuple[CandidateRelease, dict[str, Any]]:
    root = repo_root(app_root)
    if root is None:
        return release, {"ok": False, "reason": "ambiguous_repo_root"}
    if not release.candidate_commit:
        return release, {"ok": False, "reason": "missing_candidate_commit"}
    source_candidate_commit = release.candidate_commit
    main_head = current_commit(root)
    if not main_head:
        return release, {"ok": False, "reason": "promoted_head_missing"}

    promotion_method: str
    merge: dict[str, Any] | None = None
    cherry: dict[str, Any] | None = None
    if main_head == release.parent_commit:
        merge = run_git(["merge", "--ff-only", source_candidate_commit], root, timeout=timeout)
        if merge["returncode"] != 0:
            return release, {"ok": False, "reason": "promotion_fast_forward_failed", "merge": merge}
        promotion_method = "fast_forward"
    else:
        candidate_paths, candidate_paths_result = changed_paths_between_commits(
            root, release.parent_commit, source_candidate_commit, timeout=timeout
        )
        main_paths, main_paths_result = changed_paths_between_commits(
            root, release.parent_commit, main_head, timeout=timeout
        )
        if candidate_paths is None or main_paths is None:
            return release, {
                "ok": False,
                "reason": "promotion_changed_paths_failed",
                "candidate_paths": candidate_paths_result,
                "main_paths": main_paths_result,
            }
        overlapping_paths = sorted(candidate_paths & main_paths)
        if overlapping_paths:
            release.status = "promotion_overlap_requires_repair"
            return release, {
                "ok": False,
                "reason": "promotion_overlap_requires_repair",
                "source_candidate_commit": source_candidate_commit,
                "parent_commit": release.parent_commit,
                "main_head": main_head,
                "candidate_changed_paths": sorted(candidate_paths),
                "main_changed_paths": sorted(main_paths),
                "overlapping_paths": overlapping_paths,
            }

        cherry = run_git(["cherry-pick", source_candidate_commit], root, timeout=timeout)
        if cherry["returncode"] != 0:
            abort = run_git(["cherry-pick", "--abort"], root, timeout=timeout)
            return release, {
                "ok": False,
                "reason": "promotion_cherry_pick_failed",
                "cherry_pick": cherry,
                "cherry_pick_abort": abort,
                "candidate_changed_paths": sorted(candidate_paths),
                "main_changed_paths": sorted(main_paths),
            }
        promotion_method = "disjoint_cherry_pick"
    promoted_commit = current_commit(root)
    if not promoted_commit:
        return release, {"ok": False, "reason": "promoted_head_missing", "merge": merge}
    champion = update_champion_latest(root, promoted_commit, timeout=timeout) if update_champion else {
        "ok": True,
        "status": "deferred_by_policy",
        "champion_tag": "champion/latest",
        "champion_commit": None,
    }
    if not champion["ok"]:
        return release, {"ok": False, "reason": champion["reason"], "champion": champion}
    release.status = "promoted"
    release.promotion_reason = "candidate passed deterministic sandbox gates"
    return release, {
        "ok": True,
        "status": "promoted",
        "promotion_method": promotion_method,
        "champion_tag": champion["champion_tag"],
        "champion_update_deferred": not update_champion,
        "source_candidate_commit": source_candidate_commit,
        "promoted_commit": promoted_commit,
        "merge": merge,
        "cherry_pick": cherry,
        "champion": champion,
    }


def cleanup_worktree(release: CandidateRelease, app_root: pathlib.Path, *, timeout: int = 120) -> dict[str, Any]:
    root = repo_root(app_root)
    if root is None:
        return {"ok": False, "reason": "ambiguous_repo_root"}
    remove = run_git(["worktree", "remove", "--force", release.worktree_path], root, timeout=timeout)
    prune = run_git(["worktree", "prune"], root, timeout=timeout)
    return {"ok": remove["returncode"] == 0, "remove": remove, "prune": prune}
