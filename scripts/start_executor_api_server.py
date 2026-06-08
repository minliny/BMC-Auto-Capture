#!/usr/bin/env python3
"""
Start the Executor API server for receiving dispatched jobs.

Usage:
  python scripts/start_executor_api_server.py
  python scripts/start_executor_api_server.py --host 0.0.0.0 --port 8765
  python scripts/start_executor_api_server.py --executor-id exec-win-001 --callback-transport http

Windows PowerShell:
  python scripts/start_executor_api_server.py --host 0.0.0.0 --port 8765 --executor-id exec-win-001 --callback-transport http
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Ensure project root is on path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))


def main():
    parser = argparse.ArgumentParser(
        description="BMC Auto-Capture Executor API Server v0.1"
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Listen host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="Listen port (default: 8765)",
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
        "--output", default="./output_api_direct",
        help="Output directory for real runner artifacts (default: ./output_api_direct)",
    )
    args = parser.parse_args()

    use_http = args.callback_transport == "http"
    use_real = args.runner == "real"

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
    )
    service.start_background_worker()

    app = create_app(service)

    transport_label = "HTTP (real)" if use_http else "Fake (test)"
    runner_label = "RealRunnerAdapter (BMC/SSH)" if use_real else "FakeRunner (dry-run)"
    print(f"Executor API server starting:")
    print(f"  Executor ID : {args.executor_id}")
    print(f"  Listen      : {args.host}:{args.port}")
    print(f"  Runner      : {runner_label}")
    print(f"  Callback    : {transport_label}")
    print(f"  Output      : {args.output}")
    print(f"  Docs        : http://{args.host}:{args.port}/docs")
    print(f"  Endpoints:")
    print(f"    POST /executor/v1/jobs          — receive dispatched job")
    print(f"    GET  /executor/v1/jobs/{{job_id}} — query job status")
    print(f"    GET  /executor/v1/status         — executor health")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
