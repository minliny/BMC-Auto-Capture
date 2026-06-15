#!/usr/bin/env python3
"""Start the shared Executor API server entry point.

This script is kept for compatibility with existing docs and operator habits.
Server arguments are owned by ``src.cli.server`` and are shared with
``run.py --server``.
"""

from __future__ import annotations

import sys
from pathlib import Path


_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

_app_dir = _project_root / "app"
if _app_dir.is_dir() and str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))


def main() -> int:
    from src.cli.server import server_main

    app_dir = _app_dir if _app_dir.is_dir() else _project_root
    return server_main(sys.argv[1:], app_dir=app_dir)


if __name__ == "__main__":
    raise SystemExit(main())
