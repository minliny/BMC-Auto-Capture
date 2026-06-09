"""
CLI entry point: python -m bmc_auto_capture --excel <path> [--config <path>]
                或通过 启动.cmd 调用 bmc-auto-capture.exe --launcher [...]
"""

from __future__ import annotations
import logging
import os
import sys
from pathlib import Path

from .cli.args import build_parser
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

    parser = build_parser()
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

    # Preflight-only mode
    if args.preflight_only:
        from src.loader.excel_reader import load_all as _load
        from src.connectivity.preflight import check_all as _preflight_all
        from src.connectivity.preflight import check_auth_all as _preflight_auth
        from src.out.collector import write_preflight_auth_csv
        devices, tasks = _load(str(excel_path))

        if args.preflight_auth:
            # Credential check mode
            target = args.preflight_auth or "all"
            print(f"\nPreflight (auth): loaded {len(devices)} rows from Excel, target={target}")
            print(f"  checking credentials...\n")
            max_w = config.max_bmc_workers + config.max_ssh_workers
            report = _preflight_auth(devices, timeout=config.tcp_connect_timeout,
                                     max_workers=max_w, target=target)
            # Write auth result CSV
            try:
                p = write_preflight_auth_csv(report, config.output_root)
                print(f"\nAuth check results saved to: {p}")
            except Exception as e:
                print(f"WARNING: Failed to write auth CSV: {e}")
        else:
            # Network connectivity check mode
            target = args.preflight_target or "all"
            print(f"\nPreflight (connectivity): loaded {len(devices)} rows from Excel, target={target}")
            print(f"  checking unique enabled devices...\n")
            max_w = config.max_bmc_workers + config.max_ssh_workers
            report = _preflight_all(devices, timeout=config.tcp_connect_timeout,
                                    max_workers=max_w, target=target)

        # Print summary
        if args.preflight_auth:
            ok = report.bmc_ok + report.ssh_ok
            total = report.total
            print(f"\nAuth check: {ok}/{total} passed")
            if ok < total:
                sys.exit(1)
        sys.exit(0)

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
