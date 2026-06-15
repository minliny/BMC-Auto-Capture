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
import importlib
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


def _arg_value(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _resolve_app_dir_from_argv(args: list[str]) -> Path:
    explicit = _arg_value(args, "--app-dir")
    if explicit:
        return Path(explicit).resolve()
    if getattr(sys, "frozen", False):
        release_app = (_exe_dir().parent / "app").resolve()
        local_app = (_exe_dir() / "app").resolve()
        return release_app if release_app.is_dir() else local_app
    project_root = Path(__file__).resolve().parent
    app_candidate = project_root / "app"
    return (app_candidate if app_candidate.is_dir() else project_root).resolve()


def _prepend_app_dir(app_dir: Path) -> None:
    if app_dir.is_dir():
        app_dir_s = str(app_dir)
        if app_dir_s not in sys.path:
            sys.path.insert(0, app_dir_s)


def _import_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _is_playwright_browsers_dir(d: Path) -> bool:
    """检查目录是否实际包含 Playwright 浏览器文件。

    有效目录必须包含至少一个 chromium-* / chrome-win / msedge-* / firefox-* / webkit-* 子目录。
    仅存在空 ms-playwright 目录不算有效。
    """
    if not d.is_dir():
        return False
    try:
        for child in d.iterdir():
            if child.is_dir():
                name = child.name.lower()
                if any(name.startswith(prefix) for prefix in (
                    "chromium-", "chrome-win", "msedge-", "firefox-", "webkit-",
                )):
                    return True
    except (OSError, PermissionError):
        return False
    return False


def _setup_browser_path():
    """Set PLAYWRIGHT_BROWSERS_PATH.

    Priority:
    1. runtime/playwright_browsers/ (full zip layout)
    2. runtime-layer/playwright_browsers/ (RC split layout)
    3. ../runtime/playwright_browsers/ (fallback: parent dir)
    4. ../runtime-layer/playwright_browsers/ (RC split, fallback: parent dir)
    5. playwright_browsers/ next to exe (flat frozen layout)
    6. ../playwright_browsers/ (old frozen layout)
    7. PLAYWRIGHT_BROWSERS_PATH env var (if already set AND valid)
    8. System ms-playwright cache (must actually contain browser files)
    9. If all fail, UNSET stale env var so Playwright gives a clear error
    """
    _print = print  # Use builtin print (logging may not be set up yet)

    # 1. Search bundled browsers — ordered by priority, all verified
    search_dirs = [
        _exe_dir() / "runtime" / "playwright_browsers",            # project_root/runtime/playwright_browsers/
        _exe_dir() / "runtime-layer" / "playwright_browsers",     # RC split: runtime-layer/playwright_browsers/
        _exe_dir().parent / "runtime" / "playwright_browsers",    # fallback: parent/runtime/playwright_browsers/
        _exe_dir().parent / "runtime-layer" / "playwright_browsers",  # fallback: parent/runtime-layer/playwright_browsers/
        _exe_dir() / "playwright_browsers",                       # frozen: next to exe
        _exe_dir().parent / "playwright_browsers",                # frozen: one level up
    ]

    for d in search_dirs:
        if _is_playwright_browsers_dir(d):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(d.resolve())
            _print("[browser] Using bundled Playwright browsers")
            return

    # 2. Existing env var — only if the path actually contains browsers
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env_path:
        ep = Path(env_path)
        if _is_playwright_browsers_dir(ep):
            _print("[browser] Using PLAYWRIGHT_BROWSERS_PATH")
            return
        else:
            _print("[browser] WARNING: PLAYWRIGHT_BROWSERS_PATH exists but has no browser files")

    # 3. System ms-playwright cache — must contain actual browser files, not just an empty dir
    for cache in [
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]:
        if _is_playwright_browsers_dir(cache):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache.resolve())
            _print("[browser] Using system Playwright cache")
            return
        elif cache.is_dir():
            _print("[browser] WARNING: system Playwright cache exists but has no browser files")

    # 4. Last resort: if env var was set but invalid, unset it
    if env_path:
        _print("[browser] WARNING: Unsetting stale PLAYWRIGHT_BROWSERS_PATH")
        del os.environ["PLAYWRIGHT_BROWSERS_PATH"]

    _print("[browser] WARNING: No Playwright browsers found. Install: python -m playwright install chromium")


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


def _run_launcher_mode(args_list: list[str]) -> int:
    """Run launcher.main() with the given args."""
    app_dir = _resolve_app_dir_from_argv(args_list)
    _prepend_app_dir(app_dir)

    try:
        launcher_main = _import_attr("src.cli.launcher", "main")
        return launcher_main()
    except ImportError as e:
        print(f"ERROR: launcher 模块不可用: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: launcher 执行失败: {e}", file=sys.stderr)
        return 1


def main():
    # Skip browser setup print if only doing auth preflight (no browser needed)
    _is_preflight_auth = "--preflight-auth" in sys.argv
    if not _is_preflight_auth:
        _setup_browser_path()
    else:
        # Still set env var for browser path, but don't print
        import os as _os
        for _d in [__import__("pathlib").Path(__file__).resolve().parent / "runtime" / "playwright_browsers"]:
            if _d.is_dir():
                _os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_d.resolve())
                break
    _setup_encoding()
    initial_app_dir = _resolve_app_dir_from_argv(sys.argv[1:])
    _prepend_app_dir(initial_app_dir)

    # --- Server mode: Executor API (replaces legacy Network Boot API) ---
    if "--server" in sys.argv:
        server_parser = argparse.ArgumentParser(add_help=False)
        server_parser.add_argument("--server", action="store_true", default=True)
        server_parser.add_argument("--app-dir", default=None)
        server_parser.add_argument("--host", default="127.0.0.1")
        server_parser.add_argument("--port", type=int, default=8080)
        server_parser.add_argument("--log-level", default="info")
        server_parser.add_argument("--runner", default="fake", choices=("fake","real"))
        server_parser.add_argument("--callback-transport", default="fake", choices=("fake","http"))
        server_parser.add_argument("--executor-id", default="exec-default")
        server_parser.add_argument("--enable-real-runner", action="store_true",
                                   help="Allow API requests to execute real BMC/SSH tasks")
        server_parser.add_argument("--enable-debug-callback-receiver", action="store_true",
                                   help="Enable built-in debug callback receiver at /debug/plan-item-statuses")
        server_parser.add_argument("--legacy-network-boot", action="store_true",
                                   help="Start legacy Network Boot API instead of Executor API")
        server_args, _ = server_parser.parse_known_args()

        # Legacy mode (explicit opt-in)
        if server_args.legacy_network_boot:
            start_minimal_server = _import_attr("api.boot", "start_minimal_server")
            start_minimal_server(
                host=server_args.host, port=server_args.port,
                log_level=server_args.log_level, app_dir=str(initial_app_dir),
            )
            return 0

        # New Executor API (default)
        DirectDispatchService = _import_attr(
            "src.executor_api_server.service", "DirectDispatchService",
        )
        create_app = _import_attr("src.executor_api_server.app", "create_app")
        PlanRunService = _import_attr("src.plan_run_service", "PlanRunService")
        import uvicorn

        use_http = server_args.callback_transport == "http"
        use_real = server_args.runner == "real"
        if use_real and not server_args.enable_real_runner:
            print("ERROR: --runner real requires --enable-real-runner", file=sys.stderr)
            return 2

        svc = DirectDispatchService(
            executor_id=server_args.executor_id,
            use_http_callback=use_http, runner_mode="real" if use_real else "fake",
            allow_real_runner=server_args.enable_real_runner,
        )
        svc.start_background_worker()

        prs = PlanRunService(
            use_http_callback=use_http,
            allow_real_runner=server_args.enable_real_runner,
        )

        app = create_app(svc, plan_run_service=prs,
                         debug_callback_receiver=server_args.enable_debug_callback_receiver)

        print(f"Executor API server starting (legacy compat enabled):")
        print(f"  host={server_args.host} port={server_args.port}")
        print(f"  runner={server_args.runner} callback={server_args.callback_transport}")
        print(f"  realRunnerEnabled={server_args.enable_real_runner}")
        print(f"  Legacy endpoints: /health /version /network/ping /routes")
        print(f"  Executor endpoints: /executor/v1/status /executor/v1/plans/...")
        if server_args.enable_debug_callback_receiver:
            print(f"  Debug callback: POST/GET/DELETE /debug/plan-item-statuses")

        uvicorn.run(app, host=server_args.host, port=server_args.port,
                    log_level=server_args.log_level)

    # 检查是否为 launcher 模式 (由 启动.cmd 调用)
    if "--launcher" in sys.argv:
        clean_args = [a for a in sys.argv[1:] if a != "--launcher"]
        sys.argv = [sys.argv[0]] + clean_args
        return _run_launcher_mode(clean_args)

    build_parser = _import_attr("src.cli.args", "build_parser")
    resolve_execution_cli = _import_attr("src.cli.args", "resolve_execution_cli")
    parser = build_parser()
    # --excel is optional in shared parser; run.py validates below
    args = parser.parse_args()

    # Resolve app directory
    if args.app_dir:
        app_dir = Path(args.app_dir).resolve()
    elif getattr(sys, "frozen", False):
        # Frozen: app/ is ../app relative to exe (exe is in runtime/)
        app_dir = (_exe_dir().parent / "app").resolve()
    else:
        # Source repo: try ./app first (release layout), then project root (dev layout)
        project_root = Path(__file__).resolve().parent
        app_candidate = project_root / "app"
        app_dir = app_candidate if app_candidate.is_dir() else project_root

    if not app_dir.is_dir():
        print(f"ERROR: app directory not found: {app_dir}", file=sys.stderr)
        print(f"  Current working directory: {Path.cwd()}", file=sys.stderr)
        print(f"  Tried: {app_dir}", file=sys.stderr)
        print(f"  Usage: python run.py --app-dir <path_to_app>", file=sys.stderr)
        sys.exit(1)

    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    AppConfig = _import_attr("src.models.app_config", "AppConfig")
    PipelineApp = _import_attr("src.app", "App")

    effective_mode, effective_max_bmc_workers, effective_max_ssh_workers, _legacy_concurrency = (
        resolve_execution_cli(args, sys.argv[1:])
    )

    # Config
    config_path = args.config or app_dir / "config" / "default_config.yaml"
    if not Path(config_path).exists():
        config_path = _bundle_dir() / "config" / "default_config.yaml"
    if Path(config_path).exists():
        config = AppConfig.from_yaml(config_path)
    else:
        print(f"WARNING: Config not found at {config_path}, using defaults")
        config = AppConfig()

    # --- Apply CLI overrides ---
    cli_changes = config.apply_cli_overrides(
        output_root=args.output,
        max_bmc_workers=effective_max_bmc_workers,
        max_ssh_workers=effective_max_ssh_workers,
        ssh_command_timeout=args.ssh_command_timeout,
        ssh_idle_timeout=args.ssh_idle_timeout,
        bmc_page_timeout=args.bmc_page_timeout,
        bmc_artifact_profile=args.bmc_artifact_profile,
        no_preflight=args.no_preflight,
    )

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- Configuration summary ---
    print("─" * 60)
    print("  Configuration")
    print("─" * 60)
    print(f"  config file   : {config_path}")
    print(f"  output_root   : {config.output_root}")
    print(f"  mode          : {effective_mode}")
    print(f"  max_bmc_workers: {config.max_bmc_workers}")
    print(f"  max_ssh_workers: {config.max_ssh_workers}")
    print(f"  ssh_command_timeout: {config.ssh_command_timeout}s")
    print(f"  ssh_idle_timeout   : {config.ssh_idle_timeout}s")
    print(f"  bmc_page_timeout   : {config.bmc_page_timeout}s")
    print(f"  bmc_artifact_profile: {config.bmc_artifact_profile}")
    print(f"  preflight_enabled  : {config.preflight_enabled}")
    if cli_changes:
        print(f"  CLI overrides ({len(cli_changes)}):")
        for c in cli_changes:
            print(f"    - {c}")
    print("─" * 60)

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
        _load = _import_attr("src.loader.excel_reader", "load_all")
        _preflight_all = _import_attr("src.connectivity.preflight", "check_all")
        _preflight_auth = _import_attr("src.connectivity.preflight", "check_auth_all")
        devices, tasks = _load(str(excel_path))

        if args.preflight_auth:
            # Credential check mode
            target = args.preflight_auth or "all"
            print(f"\nPreflight (auth): loaded {len(devices)} rows from Excel, target={target}")
            print(f"  checking credentials...\n")
            max_w = config.max_bmc_workers + config.max_ssh_workers
            report = _preflight_auth(devices, timeout=config.tcp_connect_timeout,
                                     max_workers=max_w, target=target)
        else:
            # Network connectivity check mode (TCP probe)
            target = args.preflight_target or "all"
            print(f"\nPreflight (connectivity): loaded {len(devices)} rows from Excel, target={target}")
            print(f"  checking unique enabled devices...\n")
            max_w = config.max_bmc_workers + config.max_ssh_workers
            report = _preflight_all(devices, timeout=config.tcp_connect_timeout,
                                    max_workers=max_w, target=target)

        # Build device lookup for group info
        dev_lookup: dict[str, str] = {}
        for d in devices:
            if d.device_name not in dev_lookup:
                dev_lookup[d.device_name] = d.device_group

        # Group results by device_group
        from collections import defaultdict
        groups: dict[str, dict] = defaultdict(lambda: {
            "devices": set(),
            "bmc_ok": 0, "bmc_no_ip": [], "bmc_fail": defaultdict(list),
            "ssh_ok": 0, "ssh_no_ip": [], "ssh_fail": defaultdict(list),
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
                g["bmc_no_ip"].append(r)
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

            # SSH
            if r.ssh_status == "OK":
                g["ssh_ok"] += 1
                g["ssh_with_ip"].add(r.device_name)
            elif r.ssh_status == "IP_EMPTY":
                g["ssh_no_ip"].append(r)
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

        # Print per-group summary
        print(f"\n{'=' * 80}")
        print(f"  Connectivity Preflight — Per-Group Summary")
        print(f"{'=' * 80}")

        for group_name in sorted(groups.keys()):
            g = groups[group_name]
            total_dev = len(g["devices"])

            print(f"\n  [{group_name}]  ({total_dev} devices)")

            # BMC
            print(f"    ── 带外 (BMC) :443 ──")
            print(f"    设备总数: {total_dev}")
            print(f"    已配置BMC IP: {len(g['bmc_with_ip'])} 台")
            print(f"    未配置BMC IP: {len(g['bmc_no_ip'])} 台（仅带内设备）")
            print(f"    连通测试通过: {g['bmc_ok']}")
            bmc_fail_count = sum(len(v) for v in g["bmc_fail"].values())
            if bmc_fail_count > 0:
                print(f"    连通测试不通过: {bmc_fail_count}")
                for cat, items in sorted(g["bmc_fail"].items(), key=lambda x: -len(x[1])):
                    print(f"      └ {cat}: {len(items)} 台")
                    for r in items[:3]:
                        print(f"         · {r.device_name}: {r.bmc_error}")
                    if len(items) > 3:
                        print(f"         · ... and {len(items) - 3} more")
            else:
                print(f"    连通测试不通过: 0")

            # SSH
            print(f"    ── 带内 (SSH) :22 ──")
            print(f"    设备总数: {total_dev}")
            print(f"    已配置带内IP: {len(g['ssh_with_ip'])} 台")
            print(f"    未配置带内IP: {len(g['ssh_no_ip'])} 台（仅带外设备）")
            print(f"    连通测试通过: {g['ssh_ok']}")
            ssh_fail_count = sum(len(v) for v in g["ssh_fail"].values())
            if ssh_fail_count > 0:
                print(f"    连通测试不通过: {ssh_fail_count}")
                for cat, items in sorted(g["ssh_fail"].items(), key=lambda x: -len(x[1])):
                    print(f"      └ {cat}: {len(items)} 台")
                    for r in items[:3]:
                        print(f"         · {r.device_name}: {r.ssh_error}")
                    if len(items) > 3:
                        print(f"         · ... and {len(items) - 3} more")
            else:
                print(f"    连通测试不通过: 0")

        # Overall
        all_bmc_ok = sum(g["bmc_ok"] for g in groups.values())
        all_bmc_fail = sum(sum(len(v) for v in g["bmc_fail"].values()) for g in groups.values())
        all_bmc_no_ip = sum(len(g["bmc_no_ip"]) for g in groups.values())
        all_ssh_ok = sum(g["ssh_ok"] for g in groups.values())
        all_ssh_fail = sum(sum(len(v) for v in g["ssh_fail"].values()) for g in groups.values())
        all_ssh_no_ip = sum(len(g["ssh_no_ip"]) for g in groups.values())
        print(f"\n  {'─' * 70}")
        print(f"  TOTAL: {report.total} devices")
        print(f"  BMC: {all_bmc_ok} OK / {all_bmc_fail} FAIL / {all_bmc_no_ip} 未配置IP")
        print(f"  SSH: {all_ssh_ok} OK / {all_ssh_fail} FAIL / {all_ssh_no_ip} 未配置IP")
        print(f"{'=' * 80}")
        sys.exit(0)

    # Run
    app = PipelineApp(config)
    results = app.run(str(excel_path), mode=effective_mode)

    if not results:
        sys.exit(1)
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS", "EXEC_SKIPPED_PRECHECK_FAILED"))
    sys.exit(1 if failed > len(results) * 0.5 else 0)


if __name__ == "__main__":
    main()
