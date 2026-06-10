"""
FastAPI application for the Executor API v1 + plan/run dispatch + legacy compat.
"""

from __future__ import annotations
import hashlib
import logging
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import (
    JobDispatchRequest, JobAcceptResponse, JobStatusResponse, ExecutorStatusResponse,
)
from .service import DirectDispatchService, ValidationError

logger = logging.getLogger("bmc_auto_capture.executor_api")

# Shared store for debug callback receiver (thread-safe, in-memory)
_debug_callback_store: list[dict] = []
_debug_callback_lock = threading.Lock()

# Managed config directory for uploaded Excel files
_config_dir: Path | None = None
_config_dir_lock = threading.Lock()


def create_app(
    service: DirectDispatchService,
    run_service=None,  # RunDispatchService, optional
    plan_run_service=None,  # PlanRunService, optional
    debug_callback_receiver: bool = False,  # Enable built-in debug callback receiver
    managed_config_dir: str | None = None,  # Dir for uploaded Excel files
) -> FastAPI:
    app = FastAPI(title="BMC Auto-Capture Executor API v0.2", version="0.2.4")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # ==================================================================
    # Legacy compat routes (replaces api/boot.py)
    # ==================================================================
    _register_legacy_routes(app)

    # ==================================================================
    # Debug callback receiver (方案 B — built-in, no external Python needed)
    # ==================================================================

    if debug_callback_receiver:
        _register_debug_callback_receiver(app)

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
            "version": "0.2.4",
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
    """Plan run routes: Excel config, run start, query, and remote upload."""

    @app.post("/executor/v1/config/excel:path")
    async def set_latest_excel_path(req: Request):
        """Set latest Excel by executor-local path (local debug only)."""
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400,
                                detail={"code": "INVALID_JSON_BODY", "message": "Request body must be valid JSON"})
        path = body.get("excelPath", "")
        if not path:
            raise HTTPException(status_code=400, detail={"code": "EXCEL_PATH_REQUIRED",
                                                         "message": "excelPath is required"})
        result = prs.set_latest_excel(path)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400)
        return result

    @app.post("/executor/v1/config/excel")
    async def upload_excel(file: UploadFile = File(...)):
        """Upload Excel file from remote Windows client (multipart/form-data)."""
        # Validate content type / extension
        filename = file.filename or "upload.xlsx"
        if not filename.lower().endswith(".xlsx"):
            return JSONResponse(
                status_code=400,
                content={"accepted": False, "code": "INVALID_EXCEL_FILE",
                         "message": "Only .xlsx files are accepted"},
            )

        raw = await file.read()
        if not raw or len(raw) < 100:
            return JSONResponse(
                status_code=400,
                content={"accepted": False, "code": "EMPTY_EXCEL_FILE",
                         "message": "Excel file is empty or too small"},
            )

        # Compute sha256
        sha = hashlib.sha256(raw).hexdigest()

        # Determine managed config directory
        global _config_dir
        if _config_dir is None:
            # Default to .runtime/configs/ relative to CWD or exe dir
            for base in [Path.cwd(), Path(__file__).resolve().parent.parent.parent]:
                cand = base / ".runtime" / "configs"
                try:
                    cand.mkdir(parents=True, exist_ok=True)
                    _config_dir = cand
                    break
                except (OSError, PermissionError):
                    continue

        if _config_dir is None:
            return JSONResponse(
                status_code=500,
                content={"accepted": False, "code": "CONFIG_DIR_ERROR",
                         "message": "Cannot create managed config directory"},
            )

        # Save to history
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        history_name = f"{ts}_{filename}"
        history_path = _config_dir / history_name
        try:
            _config_dir.mkdir(parents=True, exist_ok=True)
            history_path.write_bytes(raw)
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"accepted": False, "code": "FILE_WRITE_ERROR",
                         "message": str(e)},
            )

        # Save as latest.xlsx
        latest_path = _config_dir / "latest.xlsx"
        try:
            latest_path.write_bytes(raw)
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"accepted": False, "code": "FILE_WRITE_ERROR",
                         "message": str(e)},
            )

        # Parse with Excel loader
        try:
            result = prs.set_latest_excel(str(latest_path))
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"accepted": False, "code": "INVALID_EXCEL_CONFIG",
                         "message": f"Excel parsing failed: {e}"},
            )

        if not result.get("accepted"):
            return JSONResponse(status_code=400, content=result)

        return {
            **result,
            "filename": filename,
            "excelHash": sha,
            "sha256": sha,
            "storedPath": str(latest_path),
            "message": "excel config uploaded and accepted as latest",
        }

    @app.get("/executor/v1/config/latest")
    async def get_latest_config():
        """Return info about the latest Excel config, or hasLatest=false."""
        from ..plan_run_service.service import _get_latest_excel
        info = _get_latest_excel()
        if info is None:
            return {"hasLatest": False}
        sha256_val = info.get("sha256", "")
        return {
            "hasLatest": True,
            "excelHash": sha256_val,
            "configVersion": info.get("configVersion", ""),
            "filename": os.path.basename(info.get("path", "")),
            "sha256": sha256_val,
            "deviceCount": info.get("deviceCount", 0),
            "enabledDeviceCount": info.get("enabledDeviceCount", 0),
            "taskCount": info.get("taskCount", 0),
            "enabledTaskCount": info.get("enabledTaskCount", 0),
            "source": "uploaded" if _config_dir and info.get("path", "").startswith(str(_config_dir)) else "path",
            "storedPath": info.get("path", ""),
            "createdAt": "",
        }

    @app.post("/executor/v1/plans/{plan_id}:run")
    async def start_plan_run(plan_id: int, req: Request):
        """Start a plan run. Handles empty/non-JSON body gracefully."""
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400,
                                detail={"code": "INVALID_JSON_BODY",
                                        "message": "Request body must be valid JSON"})
        if not isinstance(body, dict):
            raise HTTPException(status_code=400,
                                detail={"code": "INVALID_JSON_BODY",
                                        "message": "Request body must be a JSON object"})
        result = prs.start_plan_run(plan_id, body)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400)
        return result

    @app.get("/executor/v1/plans/{plan_id}/runs/{run_id}")
    async def get_plan_run(plan_id: int, run_id: str):
        """Get plan run summary."""
        r = prs.get_run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND",
                                                         "message": f"Run not found: {run_id}"})
        return r

    @app.get("/executor/v1/plans/{plan_id}/runs/{run_id}/items")
    async def get_plan_run_items(plan_id: int, run_id: str):
        """Get plan run per-item details."""
        r = prs.get_run_items(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND",
                                                         "message": f"Run not found: {run_id}"})
        return r

    # ==================================================================
    # External Plan API (excelHash + string planId, hides runId from service)
    # ==================================================================

    @app.post("/executor/v1/plans")
    async def start_external_plan(req: Request):
        """Start external plan. Service only needs excelHash + callback URL."""
        try:
            body = await req.json()
        except Exception:
            raise HTTPException(status_code=400,
                                detail={"code": "INVALID_JSON_BODY",
                                        "message": "Request body must be valid JSON"})
        result = prs.start_external_plan(body)
        if not result.get("accepted"):
            code = 400
            em = result.get("errorMessage", "")
            if em in ("NO_LATEST_EXCEL_CONFIG", "EXCEL_HASH_MISMATCH", "MISSING_EXCEL_HASH"):
                code = 400
            return JSONResponse(content=result, status_code=code)
        return result

    @app.get("/executor/v1/plans/{plan_id}")
    async def get_external_plan(plan_id: str, request: Request):
        """Get external plan summary. Requires excelHash query param."""
        excel_hash = request.query_params.get("excelHash", "")
        if not excel_hash:
            raise HTTPException(status_code=400,
                                detail={"code": "MISSING_EXCEL_HASH",
                                        "message": "excelHash query parameter is required"})
        r = prs.get_external_plan(plan_id, excel_hash)
        if r is None:
            # Check if plan exists at all to distinguish NOT_FOUND vs HASH_MISMATCH
            from ..plan_run_service.service import PlanRun
            run = prs._get_run_by_plan_id(plan_id)
            if run is None:
                raise HTTPException(status_code=404,
                                    detail={"code": "PLAN_NOT_FOUND",
                                            "message": f"Plan not found: {plan_id}"})
            raise HTTPException(status_code=400,
                                detail={"code": "PLAN_EXCEL_HASH_MISMATCH",
                                        "message": "excelHash does not match this plan"})
        return r

    @app.get("/executor/v1/plans/{plan_id}/items")
    async def get_external_plan_items(plan_id: str, request: Request):
        """Get external plan items. Requires excelHash query param."""
        excel_hash = request.query_params.get("excelHash", "")
        if not excel_hash:
            raise HTTPException(status_code=400,
                                detail={"code": "MISSING_EXCEL_HASH",
                                        "message": "excelHash query parameter is required"})
        r = prs.get_external_plan_items(plan_id, excel_hash)
        if r is None:
            run = prs._get_run_by_plan_id(plan_id)
            if run is None:
                raise HTTPException(status_code=404,
                                    detail={"code": "PLAN_NOT_FOUND",
                                            "message": f"Plan not found: {plan_id}"})
            raise HTTPException(status_code=400,
                                detail={"code": "PLAN_EXCEL_HASH_MISMATCH",
                                        "message": "excelHash does not match this plan"})
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


# ==================================================================
# Debug callback receiver (方案 B — built-in, no external Python)
# ==================================================================

def _register_debug_callback_receiver(app: FastAPI):
    """POST /debug/plan-item-statuses — receive plan item status callbacks.
    GET  /debug/plan-item-statuses — list received callbacks.
    DELETE /debug/plan-item-statuses — clear received callbacks.

    This is a built-in debug/inspection tool that avoids needing a separate
    Python mock_plan_status_server.py.  Use it during testing by pointing
    itemStatusUrl at the Executor API itself.
    """

    @app.post("/debug/plan-item-statuses")
    async def debug_receive_plan_item_status(req: Request):
        body = await req.json()
        payload = {
            "planId": body.get("planId"),
            "deviceName": body.get("deviceName", ""),
            "taskName": body.get("taskName", ""),
            "status": body.get("status", ""),
            "updater": body.get("updater", ""),
            "errorMessage": body.get("errorMessage"),
        }
        # Preserve excelHash if sent by external Plan API callback
        if "excelHash" in body:
            payload["excelHash"] = body["excelHash"]
        entry = {
            "receivedAt": time.time(),
            "payload": payload,
        }
        with _debug_callback_lock:
            _debug_callback_store.append(entry)
        pid = payload["planId"]
        dn = payload["deviceName"]
        tn = payload["taskName"]
        st = payload["status"]
        logger.info("[debug-callback] planId=%s device=%s task=%s status=%s", pid, dn, tn, st)
        return {"ok": True}

    @app.get("/debug/plan-item-statuses")
    async def debug_list_plan_item_statuses():
        with _debug_callback_lock:
            items = list(_debug_callback_store)
        s, f = 0, 0
        for it in items:
            st = it["payload"].get("status", "")
            if st == "SUCCESS":
                s += 1
            elif st == "FAILED":
                f += 1
        return {
            "summary": {"total": len(items), "SUCCESS": s, "FAILED": f},
            "items": items,
        }

    @app.delete("/debug/plan-item-statuses")
    async def debug_clear_plan_item_statuses():
        with _debug_callback_lock:
            _debug_callback_store.clear()
        return {"ok": True, "message": "debug callback store cleared"}
