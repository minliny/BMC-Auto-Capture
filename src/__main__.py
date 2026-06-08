"""
CLI entry point: python -m bmc_auto_capture --excel <path> [--config <path>]
                或通过 启动.cmd 调用 bmc-auto-capture.exe --launcher [...]
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
from pathlib import Path

from .models.app_config import AppConfig
from .app import App


def _bundle_dir() -> Path:
    """Return the base directory, works in dev and frozen (PyInstaller) modes."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent


def _run_launcher(args: list[str]) -> int:
    """Run launcher.main() with the given args."""
    try:
        from .cli.launcher import main as launcher_main
        return launcher_main()
    except ImportError as e:
        print(f"ERROR: launcher 模块不可用: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: launcher 执行失败: {e}", file=sys.stderr)
        return 1


def main():
    # 检查是否为 launcher 模式 (由 启动.cmd 调用)
    if "--launcher" in sys.argv:
        # 移除 --launcher 标志，将剩余参数传给 launcher
        clean_args = [a for a in sys.argv[1:] if a != "--launcher"]
        sys.argv = [sys.argv[0]] + clean_args
        return _run_launcher(clean_args)

    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture v0.2.4-RC1 — 自动化测试证据采集平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  bmc-engine --excel tasks.xlsx
  bmc-engine --app-dir app --excel app/examples/_test_one_per_group.xlsx --no-preflight
  python run.py --app-dir app --excel my_tasks.xlsx --output ./results
        """,
    )
    parser.add_argument("--excel", "-e", required=True,
                        help="Path to Excel config (.xlsx)")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to YAML config (default: config/default_config.yaml)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output root directory (overrides YAML output_root)")
    parser.add_argument("--app-dir", default=None,
                        help="App directory containing src/, config/, tasks.json")
    parser.add_argument("--mode", "-m", choices=["sequential", "full"], default="sequential",
                        help="Execution mode: sequential or full (dynamic scheduler)")
    parser.add_argument("--max-bmc-workers", type=int, default=None,
                        help="Max BMC concurrent workers (overrides YAML)")
    parser.add_argument("--max-ssh-workers", type=int, default=None,
                        help="Max SSH concurrent workers (overrides YAML)")
    parser.add_argument("--ssh-command-timeout", type=float, default=None,
                        help="SSH single-command timeout in seconds")
    parser.add_argument("--ssh-idle-timeout", type=float, default=None,
                        help="SSH idle read timeout in seconds")
    parser.add_argument("--bmc-page-timeout", type=float, default=None,
                        help="BMC page timeout in seconds")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Preflight only, no task execution")
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip connectivity preflight entirely")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    # Load config — resolve bundled path if no explicit config given
    base = _bundle_dir()
    config_path = args.config or base / "config" / "default_config.yaml"
    if Path(config_path).exists():
        config = AppConfig.from_yaml(config_path)
    else:
        config = AppConfig()
        print(f"WARNING: Config not found at {config_path}, using defaults")

    # --- Apply CLI overrides ---
    config.apply_cli_overrides(
        output_root=args.output,
        max_bmc_workers=args.max_bmc_workers,
        max_ssh_workers=args.max_ssh_workers,
        ssh_command_timeout=args.ssh_command_timeout,
        ssh_idle_timeout=args.ssh_idle_timeout,
        bmc_page_timeout=args.bmc_page_timeout,
        no_preflight=args.no_preflight,
    )

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate Excel exists
    excel_path = Path(args.excel)
    if not excel_path.exists():
        print(f"ERROR: Excel file not found: {excel_path}", file=sys.stderr)
        sys.exit(1)

    # Run
    app = App(config)
    results = app.run(str(excel_path))

    # Exit code
    if not results:
        sys.exit(1)
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS", "EXEC_SKIPPED_PRECHECK_FAILED"))
    if failed > 0:
        sys.exit(1 if failed > len(results) * 0.5 else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
