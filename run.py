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
        from src.connectivity.preflight import check_all as _preflight_all
        devices, tasks = _load(str(excel_path))
        print(f"\nPreflight: loaded {len(devices)} rows from Excel, checking unique enabled devices...\n")
        max_w = config.max_bmc_workers + config.max_ssh_workers
        report = _preflight_all(devices, timeout=config.tcp_connect_timeout, max_workers=max_w)

        # Build device lookup for group info
        dev_lookup: dict[str, str] = {}
        for d in devices:
            if d.device_name not in dev_lookup:
                dev_lookup[d.device_name] = d.device_group

        # Group results by device_group
        from collections import defaultdict
        groups: dict[str, dict] = defaultdict(lambda: {
            "devices": set(),
            "bmc_ok": 0, "bmc_fail": defaultdict(list),
            "ssh_ok": 0, "ssh_fail": defaultdict(list),
            "bmc_with_ip": set(),
            "ssh_with_ip": set(),
        })

        for r in report.results:
            group = dev_lookup.get(r.device_name, "(unknown)")
            g = groups[group]
            g["devices"].add(r.device_name)

            # BMC
            if r.bmc_status == "OK":
                g["bmc_ok"] += 1
                g["bmc_with_ip"].add(r.device_name)
            elif r.bmc_status == "IP_EMPTY":
                g["bmc_fail"]["IP为空"].append(r)
            elif r.bmc_status == "HOST_RESOLVE_FAILED":
                g["bmc_fail"]["DNS解析失败"].append(r)
            elif r.bmc_status == "TIMEOUT":
                g["bmc_fail"]["连接超时"].append(r)
            elif r.bmc_status == "CONNECTION_REFUSED":
                g["bmc_fail"]["连接被拒绝"].append(r)
            elif r.bmc_status == "PORT_BLOCKED":
                g["bmc_fail"]["端口被拦截"].append(r)
            elif r.bmc_status == "UNREACHABLE":
                g["bmc_fail"]["网络不可达"].append(r)
            else:
                g["bmc_fail"][r.bmc_status].append(r)

            if r.bmc_status != "IP_EMPTY" and r.bmc_status != "UNKNOWN":
                # Has some kind of BMC address (could be wrong but not empty)
                pass
            if r.bmc_status == "OK":
                g["bmc_with_ip"].add(r.device_name)

            # SSH
            if r.ssh_status == "OK":
                g["ssh_ok"] += 1
                g["ssh_with_ip"].add(r.device_name)
            elif r.ssh_status == "IP_EMPTY":
                g["ssh_fail"]["IP为空"].append(r)
            elif r.ssh_status == "HOST_RESOLVE_FAILED":
                g["ssh_fail"]["DNS解析失败"].append(r)
            elif r.ssh_status == "TIMEOUT":
                g["ssh_fail"]["连接超时"].append(r)
            elif r.ssh_status == "CONNECTION_REFUSED":
                g["ssh_fail"]["连接被拒绝"].append(r)
            elif r.ssh_status == "PORT_BLOCKED":
                g["ssh_fail"]["端口被拦截"].append(r)
            elif r.ssh_status == "UNREACHABLE":
                g["ssh_fail"]["网络不可达"].append(r)
            else:
                g["ssh_fail"][r.ssh_status].append(r)

            if r.ssh_status == "OK":
                g["ssh_with_ip"].add(r.device_name)

        # Print per-group summary
        print(f"\n{'=' * 80}")
        print(f"  Connectivity Preflight — Per-Group Summary")
        print(f"{'=' * 80}")

        for group_name in sorted(groups.keys()):
            g = groups[group_name]
            total_dev = len(g["devices"])

            print(f"\n  [{group_name}]  ({total_dev} devices)")

            # BMC
            bmc_total = g["bmc_ok"] + sum(len(v) for v in g["bmc_fail"].values())
            print(f"    ── 带外 (BMC) :443 ──")
            print(f"    设备总数: {total_dev}  有BMC IP: {len(g['bmc_with_ip'])}  探测总数: {bmc_total}")
            print(f"    通过: {g['bmc_ok']}")
            fail_count = sum(len(v) for v in g["bmc_fail"].values())
            if fail_count > 0:
                print(f"    不通过: {fail_count}")
                for cat, items in sorted(g["bmc_fail"].items(), key=lambda x: -len(x[1])):
                    print(f"      └ {cat}: {len(items)} 台")
                    for r in items[:3]:
                        print(f"         · {r.device_name}: {r.bmc_error}")
                    if len(items) > 3:
                        print(f"         · ... and {len(items) - 3} more")
            else:
                print(f"    不通过: 0")

            # SSH
            ssh_total = g["ssh_ok"] + sum(len(v) for v in g["ssh_fail"].values())
            print(f"    ── 带内 (SSH) :22 ──")
            print(f"    设备总数: {total_dev}  有带内IP: {len(g['ssh_with_ip'])}  探测总数: {ssh_total}")
            print(f"    通过: {g['ssh_ok']}")
            fail_count = sum(len(v) for v in g["ssh_fail"].values())
            if fail_count > 0:
                print(f"    不通过: {fail_count}")
                for cat, items in sorted(g["ssh_fail"].items(), key=lambda x: -len(x[1])):
                    print(f"      └ {cat}: {len(items)} 台")
                    for r in items[:3]:
                        print(f"         · {r.device_name}: {r.ssh_error}")
                    if len(items) > 3:
                        print(f"         · ... and {len(items) - 3} more")
            else:
                print(f"    不通过: 0")

        # Overall
        all_bmc_ok = sum(g["bmc_ok"] for g in groups.values())
        all_bmc_fail = sum(sum(len(v) for v in g["bmc_fail"].values()) for g in groups.values())
        all_ssh_ok = sum(g["ssh_ok"] for g in groups.values())
        all_ssh_fail = sum(sum(len(v) for v in g["ssh_fail"].values()) for g in groups.values())
        print(f"\n  {'─' * 70}")
        print(f"  TOTAL: {report.total} devices | BMC {all_bmc_ok}/{all_bmc_ok+all_bmc_fail} OK | SSH {all_ssh_ok}/{all_ssh_ok+all_ssh_fail} OK")
        print(f"{'=' * 80}")
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
