"""
FastAPI application for the Executor API v1 + plan/run dispatch.
"""

from __future__ import annotations
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import (
    JobDispatchRequest, JobAcceptResponse, JobStatusResponse, ExecutorStatusResponse,
)
from .service import DirectDispatchService, ValidationError

logger = logging.getLogger("bmc_auto_capture.executor_api")


def create_app(
    service: DirectDispatchService,
    run_service=None,  # RunDispatchService, optional
) -> FastAPI:
    app = FastAPI(title="BMC Auto-Capture Executor API v0.2", version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # ==================================================================
    # Direct dispatch (existing)
    # ==================================================================

    @app.post("/executor/v1/jobs", response_model=JobAcceptResponse)
    async def receive_job(req: JobDispatchRequest):
        try:
            result = service.submit_job(req.model_dump())
        except ValidationError as e:
            raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})
        return JobAcceptResponse(**result)

    @app.get("/executor/v1/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_job(job_id: str):
        status = service.get_job_status(job_id)
        if status.get("status") == "NOT_FOUND":
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return JobStatusResponse(**status)

    @app.get("/executor/v1/status", response_model=ExecutorStatusResponse)
    async def get_status():
        return ExecutorStatusResponse(**service.get_executor_status())

    # ==================================================================
    # Plan import + query
    # ==================================================================

    if run_service is not None:
        _register_plan_routes(app, run_service)
        _register_run_routes(app, run_service)

    return app


def _register_plan_routes(app: FastAPI, rs):
    """POST /executor/v1/plans:import + GET /plans/{id}/*"""

    @app.post("/executor/v1/plans:import")
    async def import_plan(req: Request):
        body = await req.json()
        excel = body.get("excel_path", "")
        vj = body.get("validation_json_path", "")
        if not excel or not vj:
            raise HTTPException(status_code=400, detail="excel_path and validation_json_path required")
        result = rs.import_plan(excel, vj)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400)
        return result

    @app.get("/executor/v1/plans/{plan_id}")
    async def get_plan(plan_id: str):
        plan = rs.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"Plan not found: {plan_id}")
        return plan

    @app.get("/executor/v1/plans/{plan_id}/tasks")
    async def get_plan_tasks(plan_id: str):
        tasks = rs.get_plan_tasks(plan_id)
        if tasks is None:
            raise HTTPException(status_code=404, detail=f"Plan not found: {plan_id}")
        return {"plan_id": plan_id, "tasks": tasks}

    @app.get("/executor/v1/plans/{plan_id}/tasks/{task_id}")
    async def get_plan_task(plan_id: str, task_id: str):
        t = rs.get_plan_task(plan_id, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return t


def _register_run_routes(app: FastAPI, rs):
    """POST /executor/v1/runs + GET /runs/{id}/*"""

    @app.post("/executor/v1/runs")
    async def start_run(req: Request):
        body = await req.json()
        result = rs.start_run(body)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400 if "not_found" in str(result.get("reason","")) else 409)
        return result

    @app.get("/executor/v1/runs/{run_id}")
    async def get_run(run_id: str):
        r = rs.get_run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return r

    @app.get("/executor/v1/runs/{run_id}/tasks")
    async def get_run_tasks(run_id: str):
        tasks = rs.get_run_tasks(run_id)
        if tasks is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return {"run_id": run_id, "tasks": tasks}

    @app.get("/executor/v1/runs/{run_id}/tasks/{task_id}")
    async def get_run_task(run_id: str, task_id: str):
        t = rs.get_run_task(run_id, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return t
