#!/usr/bin/env python3
"""
Standalone entry point for PyInstaller bundling.
Works both in dev mode and frozen (PyInstaller) mode.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def main():
    # Ensure the project root is on sys.path so "src" package is findable
    proj_root = str(_bundle_dir())
    if proj_root not in sys.path:
        sys.path.insert(0, proj_root)

    from src.models.app_config import AppConfig
    from src.app import App as PipelineApp

    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture v2.0 - Automated Test Evidence Collection",
    )
    parser.add_argument("--excel", "-e", required=True, help="Path to Excel V2 config (.xlsx)")
    parser.add_argument("--config", "-c", default=None, help="Path to YAML config")
    parser.add_argument("--mode", "-m", choices=["sequential", "full"], default="sequential")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Config
    base = _bundle_dir()
    config_path = args.config or base / "config" / "default_config.yaml"
    if Path(config_path).exists():
        config = AppConfig.from_yaml(config_path)
    else:
        print(f"WARNING: Config not found at {config_path}, using defaults")
        config = AppConfig()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate Excel
    excel_path = Path(args.excel)
    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    # Run
    app = PipelineApp(config)
    results = app.run(str(excel_path))

    if not results:
        sys.exit(1)
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS", "EXEC_SKIPPED_PRECHECK_FAILED"))
    sys.exit(1 if failed > len(results) * 0.5 else 0)


if __name__ == "__main__":
    main()
