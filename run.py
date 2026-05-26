#!/usr/bin/env python3
"""
Entry point for PyInstaller bundling. Works in dev and frozen modes.

Directory layout:
  runtime/           ← bmc-engine(.exe), _internal/, playwright_browsers/
  app/               ← src/, config/, examples/, tasks.json  (--app-dir)
  启动.bat           ← calls: runtime\bmc-engine --app-dir app --excel ...
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def _bundle_dir() -> Path:
    """PyInstaller _internal directory (Python stdlib + pip deps)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _exe_dir() -> Path:
    """Directory containing the executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _setup_browser_path():
    """Set PLAYWRIGHT_BROWSERS_PATH. Browsers are next to the exe (in runtime/)."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return

    # Frozen: browsers are next to exe (runtime/playwright_browsers/)
    bundled = _exe_dir() / "playwright_browsers"
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
        return

    # Fallback: system cache
    for cache in [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]:
        if cache.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
            return


def main():
    _setup_browser_path()

    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture v2.0 - Automated Test Evidence Collection",
    )
    parser.add_argument("--excel", "-e", default=None, help="Path to Excel V2 config (.xlsx)")
    parser.add_argument("--config", "-c", default=None, help="Path to YAML config")
    parser.add_argument("--app-dir", default=None, help="App directory containing src/, config/, tasks.json")
    parser.add_argument("--mode", "-m", choices=["sequential", "full"], default="sequential")
    parser.add_argument("--preflight-only", action="store_true", help="Connectivity preflight only, no execution")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Resolve app directory
    if args.app_dir:
        app_dir = Path(args.app_dir).resolve()
    elif getattr(sys, "frozen", False):
        # Frozen: app/ is ../app relative to exe (exe is in runtime/)
        app_dir = (_exe_dir().parent / "app").resolve()
    else:
        app_dir = Path(__file__).resolve().parent

    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    from src.models.app_config import AppConfig
    from src.app import App as PipelineApp

    # Config
    config_path = args.config or app_dir / "config" / "default_config.yaml"
    if not Path(config_path).exists():
        config_path = _bundle_dir() / "config" / "default_config.yaml"
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

    # Auto-find Excel
    excel_path = None
    if args.excel:
        excel_path = Path(args.excel)
    else:
        candidates = [
            app_dir / "examples" / "task_template.xlsx",
            app_dir / "task_template.xlsx",
            Path("examples/task_template.xlsx"),
            Path("task_template.xlsx"),
        ]
        for c in candidates:
            if c.exists():
                excel_path = c
                print(f"使用默认配置: {c}")
                break

    if excel_path is None:
        print("ERROR: 未指定 Excel 配置文件。", file=sys.stderr)
        print("用法: bmc-engine --app-dir <app目录> --excel <Excel路径>", file=sys.stderr)
        print("或将 task_template.xlsx 放在 app/examples/ 或当前目录下", file=sys.stderr)
        sys.exit(1)

    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    # Preflight-only mode
    if args.preflight_only:
        from src.loader.excel_reader import load_all as _load
        from src.connectivity.preflight import check_all as _preflight_all, PreflightStatus
        devices, tasks = _load(str(excel_path))
        enabled = [d for d in devices if d.enabled]
        print(f"\n预检 {len(enabled)} 台设备 (TCP 443/22)...\n")
        report = _preflight_all(enabled, timeout=config.tcp_connect_timeout)
        for r in report.results:
            bmc = "OK" if r.bmc_status == "OK" else f"FAIL({r.bmc_status})"
            ssh = "OK" if r.ssh_status == "OK" else f"FAIL({r.ssh_status})"
            print(f"  {r.device_name:<25s}  BMC: {bmc:<20s}  SSH: {ssh:<20s}")
        print(f"\nBMC: {report.bmc_ok}/{report.bmc_ok+report.bmc_fail}  SSH: {report.ssh_ok}/{report.ssh_ok+report.ssh_fail}")
        sys.exit(0)

    # Run
    app = PipelineApp(config)
    results = app.run(str(excel_path), mode=args.mode)

    if not results:
        sys.exit(1)
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS", "EXEC_SKIPPED_PRECHECK_FAILED"))
    sys.exit(1 if failed > len(results) * 0.5 else 0)


if __name__ == "__main__":
    main()
