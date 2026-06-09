"""
FastAPI application for the Executor API v1 + plan/run dispatch + legacy compat.
"""

from __future__ import annotations
import logging
import os
import socket
from pathlib import Path

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
    plan_run_service=None,  # PlanRunService, optional
) -> FastAPI:
    app = FastAPI(title="BMC Auto-Capture Executor API v0.2", version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # ==================================================================
    # Legacy compat routes (replaces api/boot.py)
    # ==================================================================
    _register_legacy_routes(app)

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
    # Plan import + query (existing)
    # ==================================================================

    if run_service is not None:
        _register_plan_routes(app, run_service)
        _register_run_routes(app, run_service)

    # ==================================================================
    # Plan Run Item Status Callback
    # ==================================================================

    if plan_run_service is not None:
        _register_plan_run_routes(app, plan_run_service)

    return app


# ==================================================================
# Legacy compat routes
# ==================================================================

def _register_legacy_routes(app: FastAPI):
    """Register /health, /version, /network/ping, /routes — compat with api/boot.py."""

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "executor-api",
            "mode": "executor",
            "legacyCompatible": True,
        }

    @app.get("/version")
    async def version():
        info = {
            "name": "bmc-auto-capture",
            "mode": "executor-api",
            "status": "ok",
            "legacyCompatible": True,
        }
        # Try to read build_info.json
        for candidate in [
            Path(__file__).resolve().parent.parent.parent / "runtime" / "build_info.json",
            Path(os.getcwd()) / "runtime" / "build_info.json",
        ]:
            if candidate.exists():
                try:
                    import json
                    bi = json.loads(candidate.read_text(encoding="utf-8"))
                    info["version"] = bi.get("version", "")
                    info["git_commit"] = bi.get("git_commit", "")
                    info["git_branch"] = bi.get("git_branch", "")
                    info["git_tag"] = bi.get("git_tag", "")
                    info["build_time"] = bi.get("build_time", "")
                    info["workflow_run_id"] = bi.get("workflow_run_id", "")
                except Exception:
                    pass
                break
        return info

    @app.get("/network/ping")
    async def network_ping(request: Request):
        client_host = request.client.host if request.client else "unknown"
        return {
            "status": "ok",
            "message": "pong",
            "client_host": client_host,
            "server_host": socket.gethostname(),
            "server_port": request.url.port or 8080,
            "service": "executor-api",
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


def _register_plan_run_routes(app: FastAPI, prs):
    """POST /executor/v1/config/excel:path, POST /plans/{id}:run, GET /plans/{id}/runs/{rid}"""

    @app.post("/executor/v1/config/excel:path")
    async def set_latest_excel(req: Request):
        body = await req.json()
        path = body.get("excelPath", "")
        if not path:
            raise HTTPException(status_code=400, detail="excelPath is required")
        result = prs.set_latest_excel(path)
        if not result.get("accepted"):
            raise HTTPException(status_code=400, detail=result)
        return result

    @app.post("/executor/v1/plans/{plan_id}:run")
    async def start_plan_run(plan_id: int, req: Request):
        body = await req.json()
        result = prs.start_plan_run(plan_id, body)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400)
        return result

    @app.get("/executor/v1/plans/{plan_id}/runs/{run_id}")
    async def get_plan_run(plan_id: int, run_id: str):
        r = prs.get_run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return r


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
