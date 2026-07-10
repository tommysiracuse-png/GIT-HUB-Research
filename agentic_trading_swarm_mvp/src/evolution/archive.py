"""Lightweight archive reports for candidate releases."""

from __future__ import annotations

import json
import pathlib
from typing import Any


def write_candidate_archive(path: pathlib.Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata, sort_keys=True) + "\n")
