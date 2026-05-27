"""
FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import router

logger = logging.getLogger("bmc_auto_capture.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API server starting...")
    yield
    logger.info("API server shutting down...")


def create_app() -> FastAPI:
    _app = FastAPI(
        title="BMC Auto-Capture v0.2.1",
        description="智算项目 BMC/SSH 自动化测试证据采集平台 API",
        version="0.2.1",
        lifespan=lifespan,
    )

    _app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _app.include_router(router)

    return _app


app = create_app()


def start_server(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
