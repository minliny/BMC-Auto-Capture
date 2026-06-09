"""
Minimal API server boot — SERVER-NET-BOOT-001.
Provides /health, /version, /network/ping for network connectivity validation.
No task execution, no BMC/SSH, no webhook.
"""

from __future__ import annotations
import logging
import socket
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("bmc_auto_capture.boot")


def create_minimal_app(app_dir: str | None = None) -> FastAPI:
    app = FastAPI(
        title="BMC Auto-Capture — Network Boot",
        version="0.2.2",
        description="Minimal API for network connectivity validation",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/version")
    async def version():
        return {
            "name": "bmc-auto-capture",
            "mode": "api-server",
            "status": "ok",
            "app_dir": app_dir or str(Path.cwd()),
        }

    @app.get("/network/ping")
    async def network_ping(request: Request):
        client_host = request.client.host if request.client else "unknown"
        return {
            "status": "ok",
            "message": "pong",
            "client_host": client_host,
            "server_host": socket.gethostname(),
            "server_port": request.url.port or 8080,
        }

    @app.get("/routes")
    async def list_routes(request: Request):
        routes = []
        for route in request.app.routes:
            if hasattr(route, "path") and hasattr(route, "methods"):
                routes.append({
                    "path": route.path,
                    "methods": list(route.methods),
                    "name": route.name,
                })
        return {"routes": routes}

    return app


def start_minimal_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    log_level: str = "info",
    app_dir: str | None = None,
) -> None:
    import uvicorn

    if host == "0.0.0.0":
        print("=" * 60)
        print("  WARNING: API server is listening on all interfaces (0.0.0.0).")
        print("  Use only in trusted network.")
        print("=" * 60)

    app = create_minimal_app(app_dir=app_dir)
    resolved_app_dir = app_dir or str(Path.cwd())

    print("─" * 60)
    print("  API server enabled")
    print("─" * 60)
    print(f"  host           : {host}")
    print(f"  port           : {port}")
    print(f"  app_dir        : {resolved_app_dir}")
    print(f"  health URL     : http://{host}:{port}/health")
    print(f"  version URL    : http://{host}:{port}/version")
    print(f"  network ping   : http://{host}:{port}/network/ping")
    print(f"  routes         : http://{host}:{port}/routes")
    print("─" * 60)

    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    except KeyboardInterrupt:
        print("\nAPI server stopped.")


# Alias for legacy callers
start_minimal_server_legacy = start_minimal_server
