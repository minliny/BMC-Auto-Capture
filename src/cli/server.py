"""Shared Executor API server entry point.

Used by ``run.py --server`` and ``scripts/start_executor_api_server.py`` so
server-mode arguments and startup wiring have one owner.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from src._version import APP_VERSION_LABEL


def _import_attr(module_name: str, attr_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _prepend_app_dir(app_dir: Path | None) -> None:
    if app_dir and app_dir.is_dir():
        app_dir_s = str(app_dir.resolve())
        if app_dir_s not in sys.path:
            sys.path.insert(0, app_dir_s)


def build_server_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"BMC Auto-Capture Executor API Server {APP_VERSION_LABEL}"
    )
    parser.add_argument("--server", action="store_true", default=True,
                        help="Start Executor API server")
    parser.add_argument("--app-dir", default=None,
                        help="App directory containing src/, config/, tasks.json")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Listen host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Listen port (default: 8080)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"],
                        help="API server log level (default: info)")
    parser.add_argument("--runner", default="fake", choices=("fake", "real"),
                        help="API server runner mode (default: fake)")
    parser.add_argument("--callback-transport", default="fake", choices=("fake", "http"),
                        help="Callback transport: fake or http (default: fake)")
    parser.add_argument("--callback-timeout", type=float, default=30.0,
                        help="HTTP callback timeout in seconds (default: 30)")
    parser.add_argument("--executor-id", default="exec-default",
                        help="Executor identifier (default: exec-default)")
    parser.add_argument("--output", default="./output_api_direct",
                        help="Output directory for real runner artifacts")
    parser.add_argument("--enable-real-runner", action="store_true",
                        help="Allow API requests to execute real BMC/SSH tasks")
    parser.add_argument("--enable-debug-callback-receiver", action="store_true",
                        help="Enable built-in debug callback receiver")
    parser.add_argument("--legacy-network-boot", action="store_true",
                        help="Start legacy Network Boot API instead of Executor API")
    return parser


def server_main(argv: list[str] | None = None, app_dir: Path | None = None) -> int:
    parser = build_server_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    resolved_app_dir = Path(args.app_dir).resolve() if args.app_dir else app_dir
    _prepend_app_dir(resolved_app_dir)

    if args.legacy_network_boot:
        start_minimal_server = _import_attr("api.boot", "start_minimal_server")
        start_minimal_server(
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            app_dir=str(resolved_app_dir) if resolved_app_dir else None,
        )
        return 0

    use_http = args.callback_transport == "http"
    use_real = args.runner == "real"
    if use_real and not args.enable_real_runner:
        print("ERROR: --runner real requires --enable-real-runner", file=sys.stderr)
        return 2

    try:
        DirectDispatchService = _import_attr(
            "src.executor_api_server.service", "DirectDispatchService",
        )
        create_app = _import_attr("src.executor_api_server.app", "create_app")
        PlanRunService = _import_attr("src.plan_run_service", "PlanRunService")
    except ImportError as exc:
        print(f"ERROR: Cannot import executor API modules: {exc}", file=sys.stderr)
        print("Make sure the app directory contains the BMC Auto-Capture source tree.", file=sys.stderr)
        return 1

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is required to run the API server.", file=sys.stderr)
        print("Install it with: pip install uvicorn fastapi", file=sys.stderr)
        return 1

    svc = DirectDispatchService(
        executor_id=args.executor_id,
        use_http_callback=use_http,
        callback_timeout_seconds=args.callback_timeout,
        runner_mode="real" if use_real else "fake",
        output_root=args.output,
        allow_real_runner=args.enable_real_runner,
    )
    svc.start_background_worker()

    prs = PlanRunService(
        use_http_callback=use_http,
        allow_real_runner=args.enable_real_runner,
    )

    app = create_app(
        svc,
        plan_run_service=prs,
        debug_callback_receiver=args.enable_debug_callback_receiver,
    )

    print("Executor API server starting (legacy compat enabled):")
    print(f"  host={args.host} port={args.port}")
    print(f"  executorId={args.executor_id}")
    print(f"  runner={args.runner} callback={args.callback_transport}")
    print(f"  realRunnerEnabled={args.enable_real_runner}")
    print("  Legacy endpoints: /health /version /network/ping /routes")
    print("  Executor endpoints: /executor/v1/status /executor/v1/plans/...")
    if args.enable_debug_callback_receiver:
        print("  Debug callback: POST/GET/DELETE /debug/plan-item-statuses")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0
