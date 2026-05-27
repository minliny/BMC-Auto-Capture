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
    """Set PLAYWRIGHT_BROWSERS_PATH.

    Priority:
    1. Bundled browsers — next to exe, or one level up
    2. System ms-playwright cache
    3. If all fail, UNSET stale env var so Playwright gives a clear error
    """
    _print = print  # Use builtin print (logging may not be set up yet)

    # 1. Search bundled browsers
    search_dirs = [
        _exe_dir() / "playwright_browsers",           # runtime/playwright_browsers/
        _exe_dir().parent / "playwright_browsers",    # ../playwright_browsers/ (old layout)
    ]
    for d in search_dirs:
        if d.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(d)
            _print(f"[browser] Using bundled: {d}")
            return

    # 2. System cache
    for cache in [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]:
        if cache.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
            _print(f"[browser] Using system cache: {cache}")
            return

    # 3. Existing env var — only if the path actually exists
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env_path and Path(env_path).is_dir():
        _print(f"[browser] Using env var: {env_path}")
        return

    # 4. Stale/broken env var — unset it so Playwright fails with a clear message
    if env_path:
        _print(f"[browser] WARNING: PLAYWRIGHT_BROWSERS_PATH={env_path} does not exist!")
        _print("[browser] Unsetting it. Install Chromium: python -m playwright install chromium")
        del os.environ["PLAYWRIGHT_BROWSERS_PATH"]


def _setup_encoding():
    """Force UTF-8 I/O. Works in CMD (chcp 65001), PowerShell 5+, and frozen mode."""
    # Try Python 3.7+ reconfigure (best approach)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    # Fallback: wrap with UTF-8 writer for older Python / edge cases
    if sys.stdout.encoding and sys.stdout.encoding.upper() not in ('UTF-8', 'UTF8'):
        try:
            sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8',
                              errors='replace', buffering=1, closefd=False)
        except Exception:
            pass

    # Set default file encoding for open() and logging
    try:
        sys.setdefaultencoding  # type: ignore — not available in Python 3
    except AttributeError:
        pass


def main():
    _setup_browser_path()
    _setup_encoding()

    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture v0.2.1 — BMC/SSH 自动化测试证据采集平台",
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
                print(f"Using default config: {c}")
                break

    if excel_path is None:
        print("")
        print("=" * 60)
        print("  No Excel config file found automatically.")
        print("")
        print("  The Excel file contains TWO sheets:")
        print("    Sheet 1 '设备信息' — device IPs, usernames, passwords")
        print("    Sheet 2 '任务列表' — task names, groups, enabled flags")
        print("")
        print("  A template is included at:")
        print("    app/examples/task_template.xlsx")
        print("=" * 60)
        print("")
        user_input = input("  Enter path to Excel file (or press Enter to exit): ").strip()
        if user_input:
            excel_path = Path(user_input)
        else:
            print("No file specified. Exiting.")
            sys.exit(1)

    if excel_path is None or not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        print("Please check the path and try again.", file=sys.stderr)
        print("Usage: bmc-engine --excel <path_to_xlsx>", file=sys.stderr)
        sys.exit(1)

    # Preflight-only mode
    if args.preflight_only:
        from src.loader.excel_reader import load_all as _load
        from src.connectivity.preflight import check_all as _preflight_all, PreflightStatus
        devices, tasks = _load(str(excel_path))
        enabled = [d for d in devices if d.enabled]
        print(f"\nPreflight check: {len(enabled)} devices (TCP 443/22)...\n")
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
