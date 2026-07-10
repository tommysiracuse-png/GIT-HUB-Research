"""Repo-aware context packets for autonomous patch generation."""

from __future__ import annotations

import ast
import hashlib
import pathlib
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
