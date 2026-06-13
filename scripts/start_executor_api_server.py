#!/usr/bin/env python3
"""
Start the Executor API server for receiving dispatched jobs.

Usage:
  python scripts/start_executor_api_server.py
  python scripts/start_executor_api_server.py --host 127.0.0.1 --port 8080
  python scripts/start_executor_api_server.py --executor-id exec-win-001 --callback-transport http

Windows PowerShell:
  python scripts/start_executor_api_server.py --host 127.0.0.1 --port 8080 --executor-id exec-win-001 --callback-transport http
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Ensure project root and app/ are on path
# In a release artifact: bmc-auto-capture/
#   scripts/start_executor_api_server.py  ← we are here
#   app/src/                              ← source modules live here
# In dev checkout: <project_root>/src/    ← source modules live here
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
# Also add app/ for release artifact layout (src → app/src)
_app_dir = _project_root / "app"
if _app_dir.is_dir() and str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))


def main():
    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture Executor API Server v0.1"
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Listen host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Listen port (default: 8080)",
    )
    parser.add_argument(
        "--executor-id", default="exec-win-001",
        help="Executor identifier (default: exec-win-001)",
    )
    parser.add_argument(
        "--callback-transport", choices=("fake", "http"), default="fake",
        help="Callback transport: fake (test) or http (real POST to server). Default: fake",
    )
    parser.add_argument(
        "--callback-timeout", type=float, default=30.0,
        help="HTTP callback timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--runner", choices=("fake", "real"), default="fake",
        help="Job runner: fake (dry-run for testing) or real (BMC/SSH execution). Default: fake",
    )
    parser.add_argument(
        "--enable-real-runner", action="store_true",
        help="Allow API requests to execute real BMC/SSH tasks",
    )
    parser.add_argument(
        "--output", default="./output_api_direct",
        help="Output directory for real runner artifacts (default: ./output_api_direct)",
    )
    args = parser.parse_args()

    use_http = args.callback_transport == "http"
    use_real = args.runner == "real"
    if use_real and not args.enable_real_runner:
        print("ERROR: --runner real requires --enable-real-runner", file=sys.stderr)
        sys.exit(2)

    try:
        from src.executor_api_server.service import DirectDispatchService
        from src.executor_api_server.app import create_app
    except ImportError as e:
        print(f"ERROR: Cannot import executor API modules: {e}")
        print("Make sure you're running from the bmc-auto-capture project root.")
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is required to run the API server.")
        print("Install it with: pip install uvicorn")
        print("(FastAPI is also required: pip install fastapi)")
        sys.exit(1)

    service = DirectDispatchService(
        executor_id=args.executor_id,
        use_http_callback=use_http,
        callback_timeout_seconds=args.callback_timeout,
        runner_mode="real" if use_real else "fake",
        output_root=args.output,
        allow_real_runner=args.enable_real_runner,
    )
    service.start_background_worker()

    from src.plan_run_service import PlanRunService
    plan_run_service = PlanRunService(
        use_http_callback=use_http,
        allow_real_runner=args.enable_real_runner,
    )

    app = create_app(service, plan_run_service=plan_run_service)

    transport_label = "HTTP (real)" if use_http else "Fake (test)"
    runner_label = "RealRunnerAdapter (BMC/SSH)" if use_real else "FakeRunner (dry-run)"
    print(f"Executor API server starting:")
    print(f"  Executor ID : {args.executor_id}")
    print(f"  Listen      : {args.host}:{args.port}")
    print(f"  Runner      : {runner_label}")
    print(f"  Callback    : {transport_label}")
    print(f"  Real runner : {args.enable_real_runner}")
    print(f"  Docs        : http://{args.host}:{args.port}/docs")
    print(f"  Endpoints:")
    print(f"    POST /executor/v1/plans/{{id}}:run      — start plan batch")
    print(f"    GET  /executor/v1/plans/{{id}}          — query plan status")
    print(f"    GET  /executor/v1/plans/{{id}}/items    — query plan items")
    print(f"    GET  /executor/v1/status             — executor health")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
