"""
FastAPI application for the Executor API v1.
"""

from __future__ import annotations
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .schemas import (
    JobDispatchRequest,
    JobAcceptResponse,
    JobStatusResponse,
    ExecutorStatusResponse,
)
from .service import DirectDispatchService, ValidationError, DUPLICATE_COMMAND

logger = logging.getLogger("bmc_auto_capture.executor_api")


def create_app(service: DirectDispatchService) -> FastAPI:
    """Create the FastAPI app wired to a DirectDispatchService instance."""

    app = FastAPI(
        title="BMC Auto-Capture Executor API v0.1",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # POST /executor/v1/jobs
    # ------------------------------------------------------------------

    @app.post("/executor/v1/jobs", response_model=JobAcceptResponse)
    async def receive_job(req: JobDispatchRequest):
        """Receive a dispatched job from the server."""
        try:
            result = service.submit_job(req.model_dump())
        except ValidationError as e:
            raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})

        if result.get("duplicate"):
            return JobAcceptResponse(**result)

        return JobAcceptResponse(**result)

    # ------------------------------------------------------------------
    # GET /executor/v1/jobs/{job_id}
    # ------------------------------------------------------------------

    @app.get("/executor/v1/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_job(job_id: str):
        """Query a single job's status."""
        status = service.get_job_status(job_id)
        if status.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return JobStatusResponse(**status)

    # ------------------------------------------------------------------
    # GET /executor/v1/status
    # ------------------------------------------------------------------

    @app.get("/executor/v1/status", response_model=ExecutorStatusResponse)
    async def get_status():
        """Query executor health and job counts."""
        return ExecutorStatusResponse(**service.get_executor_status())

    return app
