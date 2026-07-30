"""Repo-aware context packets for autonomous patch generation."""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
from typing import Any


def _python_symbols(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
    return sorted(dict.fromkeys(symbols))[:40]


def build_builder_context(
    root: pathlib.Path,
    files: list[str],
    *,
    max_chars: int,
    likely_tests: list[str] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for rel in files:
        path = root / rel
        entry: dict[str, Any] = {"path": rel, "exists": path.exists(), "is_file": path.is_file()}
        if path.exists() and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                entry["read_error"] = str(exc)
                text = ""
            entry["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            entry["symbols"] = _python_symbols(text) if rel.endswith(".py") else []
            entry["text"] = text[:max_chars]
            entry["truncated"] = len(text) > max_chars
        else:
            entry["text"] = "<missing file>"
            entry["symbols"] = []
        entries.append(entry)
    return {
        "version": 1,
        "files": entries,
        "likely_tests": likely_tests or [],
    }


def render_builder_context(context: dict[str, Any]) -> str:
    lines = ["BUILDER_CONTEXT version=1"]
    likely_tests = context.get("likely_tests") or []
    if likely_tests:
        lines.append("LIKELY_TESTS:")
        for command in likely_tests:
            lines.append(f"- {command}")
    for entry in context.get("files") or []:
        lines.append(f"--- {entry.get('path')}")
        lines.append(f"exists={entry.get('exists')} sha256={entry.get('sha256', '')} truncated={entry.get('truncated', False)}")
        symbols = entry.get("symbols") or []
        if symbols:
            lines.append("symbols=" + ", ".join(str(symbol) for symbol in symbols))
        lines.append(str(entry.get("text") or ""))
    return "\n".join(lines)


def _tokens(value: str) -> set[str]:
    ignored = {
        "add", "build", "change", "code", "create", "file", "implement", "improve",
        "module", "paper", "proposal", "runtime", "system", "test", "update", "with",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9_]+", str(value or "").lower())
        if len(token) > 2 and token not in ignored
    }


def _imports(tree: ast.AST) -> list[str]:
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.append(node.module)
    return sorted(dict.fromkeys(values))[:80]


def build_repo_capability_map(root: pathlib.Path) -> dict[str, Any]:
    """Index actual files and symbols so models do not need to invent paths."""
    entries: list[dict[str, Any]] = []
    for base in (root / "src", root / "tests"):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            symbols = _python_symbols(text)
            imports = _imports(tree)
            entries.append(
                {
                    "path": rel,
                    "kind": "test" if rel.startswith("tests/") else "source",
                    "symbols": symbols,
                    "imports": imports,
                    "tokens": sorted(_tokens(" ".join([rel, *symbols, *imports]))),
                }
            )
    return {"version": 1, "entries": entries}


def resolve_repo_targets(
    root: pathlib.Path,
    proposal: dict[str, Any],
    *,
    conceptual_paths: list[str] | None = None,
    max_source_files: int = 4,
    max_test_files: int = 3,
) -> dict[str, Any]:
    """Resolve a behavior contract to existing source and test files."""
    query_parts = [
        str(proposal.get(key) or "")
        for key in ("title", "rationale", "proposed_change", "change_category", "market_key", "signal_key")
    ]
    query_parts.extend(conceptual_paths or [])
    query = _tokens(" ".join(query_parts))
    capability_map = build_repo_capability_map(root)
    scored: list[tuple[float, dict[str, Any]]] = []
    conceptual_stems = {
        pathlib.PurePosixPath(str(path).replace("\\", "/")).stem.lower()
        for path in conceptual_paths or []
    }
    for entry in capability_map["entries"]:
        tokens = set(entry["tokens"])
        overlap = query & tokens
        score = float(len(overlap))
        path_lower = str(entry["path"]).lower()
        score += 3.0 * sum(1 for stem in conceptual_stems if stem and stem in path_lower)
        score += 1.5 * sum(1 for token in query if token in path_lower)
        if entry["kind"] == "source":
            score += 0.25
        if score > 0.25:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
    source = [entry["path"] for _, entry in scored if entry["kind"] == "source"][:max_source_files]
    source_stems = {pathlib.PurePosixPath(path).stem.removeprefix("test_") for path in source}
    tests = [
        entry["path"]
        for _, entry in scored
        if entry["kind"] == "test"
        and (
            pathlib.PurePosixPath(entry["path"]).stem.removeprefix("test_") in source_stems
            or any(stem in entry["path"] for stem in source_stems)
        )
    ][:max_test_files]
    return {
        "query_tokens": sorted(query),
        "source_files": source,
        "test_files": tests,
        "ranked": [
            {"path": entry["path"], "score": score, "kind": entry["kind"]}
            for score, entry in scored[:12]
        ],
    }
