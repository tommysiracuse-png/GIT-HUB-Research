"""Package bootstrap for package-style test imports.

The codebase historically imports sibling modules as top-level names after
adding ``src`` to ``sys.path`` in script entrypoints and many tests. Some test
modules import code through ``src.<module>``, which executes package imports
from the repository root instead. In that mode, sibling top-level imports like
``from codex_coordination import ...`` would otherwise fail because only the
repository root, not ``src`` itself, is on ``sys.path``.

Keep package imports working by ensuring this directory is present on
``sys.path`` when the ``src`` package is imported.
"""

from __future__ import annotations

import pathlib
import sys


_SRC_DIR = pathlib.Path(__file__).resolve().parent
_src_path = str(_SRC_DIR)
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)
