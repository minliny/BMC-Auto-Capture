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

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from src._version import APP_VERSION
from .schemas import (
    JobDispatchRequest, JobAcceptResponse, JobStatusResponse, ExecutorStatusResponse,
    PlanRunCallbackConfig, PlanRunRequest, ExternalPlanRequest,
    ExcelPathRequest, ExcelConfigAcceptResponse, LatestConfigResponse,
    PlanRunAcceptResponse, PlanStatusResponse, PlanItemsResponse,
    CallbackRetryRequest, CallbackRetryResponse,
)
from .service import DirectDispatchService, ValidationError
from .contracts import get_contract_index, get_contract

logger = logging.getLogger("bmc_auto_capture.executor_api")

# Shared store for debug callback receiver (thread-safe, in-memory)
_debug_callback_store: list[dict] = []
_debug_callback_lock = threading.Lock()

# Excel config store (replaces legacy .runtime/configs/latest.xlsx)
# Writes to executor_state/configs/by_hash/ + latest.json
_config_store: object | None = None  # ExcelConfigStore, lazy init


def create_app(
    service: DirectDispatchService,
    run_service=None,  # RunDispatchService, optional
    plan_run_service=None,  # PlanRunService, optional
    debug_callback_receiver: bool = False,  # Enable built-in debug callback receiver
    managed_config_dir: str | None = None,  # Dir for uploaded Excel files
) -> FastAPI:
    app = FastAPI(title="BMC Auto-Capture Executor API v0.2", version=APP_VERSION)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # Convert Pydantic validation errors to 400 (backward compat)
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID_JSON_BODY", "message": str(exc.errors())},
        )

    # ==================================================================
    # Health endpoint (needs plan_run_service access for transport mode)
    # ==================================================================
    @app.get("/health")
    async def health():
        result = {
            "status": "ok",
            "service": "executor-api",
            "mode": "executor",
            "legacyCompatible": True,
        }
        # Expose callback transport mode for observability
        # Note: actual transport is auto-detected per request based on itemStatusUrl
        if plan_run_service is not None:
            explicit_transport = getattr(plan_run_service, '_cb_transport', None)
            if explicit_transport is not None:
                from src.plan_item_status_callback_client import HttpCallbackTransport
                result["callbackTransportMode"] = "http" if isinstance(explicit_transport, HttpCallbackTransport) else "fake"
            else:
                result["callbackTransportMode"] = "auto"
        else:
            result["callbackTransportMode"] = "none"
        return result

    # ==================================================================
    # Legacy compat routes (replaces api/boot.py)
    # ==================================================================
    _register_legacy_routes(app)

    # ==================================================================
    # Contract / schema query (read-only, no side effects)
    # ==================================================================
    _register_contract_routes(app)

    # ==================================================================
    # Debug callback receiver (方案 B — built-in, no external Python needed)
    # ==================================================================

    if debug_callback_receiver:
        _register_debug_callback_receiver(app)

    # ==================================================================
    # Direct dispatch (existing)
    # ==================================================================

    @app.post("/executor/v1/jobs", response_model=JobAcceptResponse,
              include_in_schema=False, deprecated=True)
    async def receive_job(req: JobDispatchRequest):
        try:
            result = service.submit_job(req.model_dump())
        except ValidationError as e:
            raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})
        return JobAcceptResponse(**result)

    @app.get("/executor/v1/jobs/{job_id}", response_model=JobStatusResponse,
             include_in_schema=False, deprecated=True)
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
        _register_plan_run_routes(
            app,
            plan_run_service,
            expose_internal_run_routes=run_service is None,
        )

    return app


# ==================================================================
# Legacy compat routes
# ==================================================================

def _register_legacy_routes(app: FastAPI):
    """Register /version, /network/ping, /routes — compat with api/boot.py.

    Note: /health is registered in create_app() to access plan_run_service.
    """

    @app.get("/version")
    async def version():
        info = {
            "name": "bmc-auto-capture",
            "mode": "executor-api",
            "version": APP_VERSION,
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


# ==================================================================
# Contract / schema query routes (read-only, no side effects)
# ==================================================================

def _register_contract_routes(app: FastAPI):
    """Register /executor/v1/contracts — read-only API contract queries."""

    @app.get("/executor/v1/contracts")
    async def list_contracts():
        """Return index of all available API contracts.

        Read-only: does not read Excel, connect to services, or modify state.
        """
        return get_contract_index()

    @app.get("/executor/v1/contracts/{contract_id}")
    async def get_contract_detail(contract_id: str):
        """Return detailed contract for a specific API endpoint.

        Read-only: does not read Excel, connect to services, or modify state.
        """
        contract = get_contract(contract_id)
        if contract is None:
            raise HTTPException(status_code=404,
                                detail={"code": "CONTRACT_NOT_FOUND",
                                        "message": f"Contract not found: {contract_id}"})
        return contract


def _get_config_store() -> object:
    """Lazy-init the ExcelConfigStore via shared singleton."""
    from ..excel_config_store import get_default_store
    return get_default_store()


def _config_accept_response(result: dict) -> dict:
    """Return only public config activation fields."""
    return {
        "accepted": bool(result.get("accepted")),
        "deviceCount": int(result.get("deviceCount", 0) or 0),
        "enabledDeviceCount": int(result.get("enabledDeviceCount", 0) or 0),
        "taskCount": int(result.get("taskCount", 0) or 0),
        "enabledTaskCount": int(result.get("enabledTaskCount", 0) or 0),
        "filename": result.get("filename", ""),
        "excelHash": result.get("excelHash", ""),
        "sha256": result.get("sha256", result.get("excelHash", "")),
        "message": result.get("message", ""),
    }


def _error_response(result: dict, status_code: int = 400) -> JSONResponse:
    """Return an error body without local filesystem paths."""
    body = dict(result)
    body.pop("storedPath", None)
    msg = str(body.get("message", ""))
    if "storedPath" in msg or "latest.json" in msg:
        body["message"] = "Excel config storage is unavailable or inconsistent"
    return JSONResponse(content=body, status_code=status_code)


def _register_plan_run_routes(
    app: FastAPI,
    prs,
    expose_internal_run_routes: bool = True,
):
    """Plan run routes: Excel config, run start, query, and remote upload."""

    @app.post("/executor/v1/config/excel:path", response_model=ExcelConfigAcceptResponse)
    async def set_latest_excel_path(req: ExcelPathRequest):
        """Set latest Excel by executor-local path (local debug only).

        Uses ExcelConfigStore.activate_from_local_path() which copies the
        file into executor_state/configs/by_hash/ and writes latest.json.
        Legacy .runtime/configs/latest.xlsx triggers automatic migration.
        """
        path = req.excelPath
        if not path:
            raise HTTPException(status_code=400, detail={"code": "EXCEL_PATH_REQUIRED",
                                                         "message": "excelPath is required"})
        store = _get_config_store()
        result = store.activate_from_local_path(path)
        if not result.get("accepted"):
            return _error_response(result, status_code=400)
        return _config_accept_response(result)

    @app.post("/executor/v1/config/excel", response_model=ExcelConfigAcceptResponse)
    async def upload_excel(file: UploadFile = File(...)):
        """Upload Excel file from remote Windows client (multipart/form-data).

        Uses ExcelConfigStore.activate_from_upload() which saves to
        executor_state/configs/by_hash/ and atomically writes latest.json.
        Does NOT write to .runtime/configs/latest.xlsx.
        """
        filename = file.filename or "upload.xlsx"
        store = _get_config_store()
        raw = await file.read()
        result = store.activate_from_upload(raw, filename)
        if not result.get("accepted"):
            return _error_response(result, status_code=400)
        return _config_accept_response(result)

    @app.get(
        "/executor/v1/config/latest",
        response_model=LatestConfigResponse,
        response_model_exclude_defaults=True,
    )
    async def get_latest_config():
        """Return info about the latest Excel config, or hasLatest=false.

        Reads from latest.json via ExcelConfigStore.
        Falls back to in-memory state (legacy set_latest_excel path).
        """
        store = _get_config_store()
        meta = store.get_latest()
        if isinstance(meta, dict) and meta.get("code"):
            return JSONResponse(
                content={
                    "hasLatest": False,
                    "code": meta["code"],
                    "message": "Excel config storage is unavailable or inconsistent",
                },
                status_code=409,
            )
        if meta:
            return {
                "hasLatest": True,
                "excelHash": meta.get("excelHash", ""),
                "filename": meta.get("originalFilename", ""),
                "sha256": meta.get("excelHash", ""),
                "deviceCount": meta.get("deviceCount", 0),
                "enabledDeviceCount": meta.get("enabledDeviceCount", 0),
                "taskCount": meta.get("taskCount", 0),
                "enabledTaskCount": meta.get("enabledTaskCount", 0),
                "source": meta.get("source", ""),
                "createdAt": meta.get("activatedAt", ""),
            }

        # Fallback: in-memory state from prs.set_latest_excel()
        from ..plan_run_service.service import _get_latest_excel
        info = _get_latest_excel()
        if info is None:
            return {"hasLatest": False}
        sha256_val = info.get("sha256", "")
        return {
            "hasLatest": True,
            "excelHash": sha256_val,
            "filename": os.path.basename(info.get("path", "")),
            "sha256": sha256_val,
            "deviceCount": info.get("deviceCount", 0),
            "enabledDeviceCount": info.get("enabledDeviceCount", 0),
            "taskCount": info.get("taskCount", 0),
            "enabledTaskCount": info.get("enabledTaskCount", 0),
            "source": "path",
            "createdAt": "",
        }

    @app.post("/executor/v1/plans/{plan_id}:run", response_model=PlanRunAcceptResponse)
    async def start_plan_run(plan_id: int, req: PlanRunRequest):
        """Start a plan run. Request body validated via PlanRunRequest schema."""
        body = req.model_dump()
        result = prs.start_plan_run(plan_id, body)
        if not result.get("accepted"):
            return _error_response(result, status_code=400)
        # Non-breaking observability: indicate callback transport mode
        # Auto-detect: if itemStatusUrl is provided, transport will be http
        has_url = bool(req.callback.itemStatusUrl)
        result["callbackTransportMode"] = "http" if has_url else "fake"
        return result

    @app.get("/executor/v1/plans/{plan_id}", response_model=PlanStatusResponse)
    async def get_plan(
        plan_id: str,
        request: Request,
        excelHash: str = Query(
            default="",
            description="Optional Excel SHA-256 hash. When provided, the query is validated against the plan's bound Excel hash.",
        ),
    ):
        """Get plan summary.

        Handles both:
          - int plan_id (legacy PlanRunService): no query params needed
          - str plan_id (external plan): requires excelHash query param
        """
        # Try external plan first (requires excelHash)
        excel_hash = excelHash
        if excel_hash:
            r = prs.get_external_plan(plan_id, excel_hash)
            if r is None:
                run = prs._get_plan(plan_id)
                if run is None:
                    raise HTTPException(status_code=404,
                                        detail={"code": "PLAN_NOT_FOUND",
                                                "message": f"Plan not found: {plan_id}"})
                raise HTTPException(status_code=400,
                                    detail={"code": "PLAN_EXCEL_HASH_MISMATCH",
                                            "message": "excelHash does not match this plan"})
            return r

        # Internal plan query
        key = int(plan_id) if plan_id.isdigit() else plan_id
        r = prs.get_plan(key)
        if r is None:
            raise HTTPException(status_code=404, detail={"code": "PLAN_NOT_FOUND",
                                                         "message": f"Plan not found: {plan_id}"})
        return r

    @app.get("/executor/v1/plans/{plan_id}/items", response_model=PlanItemsResponse)
    async def get_plan_items_route(
        plan_id: str,
        request: Request,
        excelHash: str = Query(
            default="",
            description="Optional Excel SHA-256 hash. When provided, the query is validated against the plan's bound Excel hash.",
        ),
    ):
        """Get plan per-item details.

        Handles both int and external (string) plan IDs.
        External plans require excelHash query param.
        """
        excel_hash = excelHash
        if excel_hash:
            r = prs.get_external_plan_items(plan_id, excel_hash)
            if r is None:
                run = prs._get_plan(plan_id)
                if run is None:
                    raise HTTPException(status_code=404,
                                        detail={"code": "PLAN_NOT_FOUND",
                                                "message": f"Plan not found: {plan_id}"})
                raise HTTPException(status_code=400,
                                    detail={"code": "PLAN_EXCEL_HASH_MISMATCH",
                                            "message": "excelHash does not match this plan"})
            return r
        key = int(plan_id) if plan_id.isdigit() else plan_id
        r = prs.get_plan_items(key)
        if r is None:
            raise HTTPException(status_code=404, detail={"code": "PLAN_NOT_FOUND",
                                                         "message": f"Plan not found: {plan_id}"})
        return r

    # ==================================================================
    # External Plan API (excelHash + string planId)
    # ==================================================================

    if expose_internal_run_routes:
        @app.get("/executor/v1/runs/{run_id}", response_model=PlanStatusResponse,
                 include_in_schema=False, deprecated=True)
        async def get_run_by_id(run_id: str):
            r = prs.get_run(run_id)
            if r is None:
                raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND",
                                                             "message": f"Run not found: {run_id}"})
            return r

        @app.get("/executor/v1/runs/{run_id}/items", response_model=PlanItemsResponse,
                 include_in_schema=False, deprecated=True)
        async def get_run_items_by_id(run_id: str):
            r = prs.get_run_items(run_id)
            if r is None:
                raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND",
                                                             "message": f"Run not found: {run_id}"})
            return r

    @app.post("/executor/v1/plans/{plan_id}/callbacks:retry", response_model=CallbackRetryResponse)
    async def retry_plan_callbacks(plan_id: str, req: CallbackRetryRequest):
        result = prs.retry_pending_callbacks(plan_id, callback_url=req.callbackUrl, mode=req.mode)
        if not result.get("accepted"):
            return _error_response(result, status_code=400)
        return result

    @app.post("/executor/v1/plans", response_model=PlanRunAcceptResponse)
    async def start_external_plan(req: ExternalPlanRequest):
        """Start external plan. Service only needs excelHash + callback URL."""
        body = req.model_dump()
        result = prs.start_external_plan(body)
        if not result.get("accepted"):
            code = 400
            em = result.get("errorMessage", "")
            if em in ("NO_LATEST_EXCEL_CONFIG", "EXCEL_HASH_MISMATCH", "MISSING_EXCEL_HASH"):
                code = 400
            return _error_response(result, status_code=code)
        # Non-breaking observability: indicate callback transport mode
        has_url = bool(req.callback.itemStatusUrl)
        result["callbackTransportMode"] = "http" if has_url else "fake"
        return result

    # GET /executor/v1/plans/{plan_id} is registered above in _register_plan_run_routes
    # (handles int plan_id).  External plan queries need excelHash query param.
    # The single handler at line 282 dispatches to prs.get_plan() for both flows.
    # External plan details are accessed via prs.get_external_plan().

    # GET /executor/v1/plans/{plan_id}/items is registered above in _register_plan_run_routes.
    # External plan items are accessed via prs.get_external_plan_items().


def _register_plan_routes(app: FastAPI, rs):
    """DEPRECATED/ISOLATED: plan import + query routes.

    These route to RunDispatchService, which is a separate flow from
    the unified planId model.  RunDispatchService uses its own run_id
    and does NOT contaminate PlanRunService, CallbackOutbox, or
    executor_state/plans/{planId}.
    """

    @app.post("/executor/v1/plans:import", include_in_schema=False, deprecated=True)
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

    @app.get("/executor/v1/plans/{plan_id}/tasks", include_in_schema=False, deprecated=True)
    async def get_plan_tasks(plan_id: str):
        """DEPRECATED/ISOLATED: RunDispatchService plan tasks.
        PlanRunService routes use /plans/{plan_id} and /plans/{plan_id}/items."""
        tasks = rs.get_plan_tasks(plan_id)
        if tasks is None:
            raise HTTPException(status_code=404, detail=f"Plan not found: {plan_id}")
        return {"plan_id": plan_id, "tasks": tasks}

    @app.get("/executor/v1/plans/{plan_id}/tasks/{task_id}", include_in_schema=False, deprecated=True)
    async def get_plan_task(plan_id: str, task_id: str):
        t = rs.get_plan_task(plan_id, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
        return t


def _register_run_routes(app: FastAPI, rs):
    """DEPRECATED/ISOLATED: POST /executor/v1/runs + GET /runs/{id}/*

    This is a legacy RunDispatchService path — NOT part of the unified
    planId model. Does NOT affect PlanRunService, CallbackOutbox, or
    executor_state/plans/{planId}. Uses its own run_id internally.
    New code should use /executor/v1/plans/{plan_id}:run instead.
    """

    @app.post("/executor/v1/runs", include_in_schema=False, deprecated=True)  # DEPRECATED
    async def start_run(req: Request):
        body = await req.json()
        result = rs.start_run(body)
        if not result.get("accepted"):
            return JSONResponse(content=result, status_code=400 if "not_found" in str(result.get("reason","")) else 409)
        return result

    @app.get("/executor/v1/runs/{run_id}", include_in_schema=False, deprecated=True)
    async def get_run(run_id: str):
        r = rs.get_run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return r

    @app.get("/executor/v1/runs/{run_id}/tasks", include_in_schema=False, deprecated=True)
    async def get_run_tasks(run_id: str):
        tasks = rs.get_run_tasks(run_id)
        if tasks is None:
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return {"run_id": run_id, "tasks": tasks}

    @app.get("/executor/v1/runs/{run_id}/tasks/{task_id}", include_in_schema=False, deprecated=True)
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
        entries = []
        if isinstance(body.get("items"), list):
            source_items = body.get("items", [])
        elif "summary" in body and not body.get("taskName"):
            source_items = []
            entries.append({
                "receivedAt": time.time(),
                "type": "summary",
                "payload": {
                    "planId": body.get("planId"),
                    "summary": body.get("summary", {}),
                },
            })
        else:
            source_items = [body]

        for raw in source_items:
            payload = {
                "planId": raw.get("planId"),
                "taskId": raw.get("taskId", ""),
                "planItemId": raw.get("planItemId", ""),
                "deviceGroup": raw.get("deviceGroup", ""),
                "deviceName": raw.get("deviceName", ""),
                "taskName": raw.get("taskName", ""),
                "status": raw.get("status", ""),
                "updater": raw.get("updater", ""),
                "errorMessage": raw.get("errorMessage"),
                "startedAt": raw.get("startedAt"),
                "finishedAt": raw.get("finishedAt"),
            }
            entries.append({
                "receivedAt": time.time(),
                "type": "item",
                "payload": payload,
            })

        with _debug_callback_lock:
            _debug_callback_store.extend(entries)
        logger.info("[debug-callback] received %d callback entrie(s)", len(entries))
        return {
            "code": 0,
            "message": "success",
            "data": {"total": len(entries), "success": len(entries), "failed": 0, "errors": []},
        }

    @app.get("/debug/plan-item-statuses")
    async def debug_list_plan_item_statuses():
        with _debug_callback_lock:
            items = list(_debug_callback_store)
        s, f = 0, 0
        for it in items:
            if it.get("type") != "item":
                continue
            st = it["payload"].get("status", "")
            if st == "SUCCESS":
                s += 1
            elif st == "FAILED":
                f += 1
        return {
            "summary": {
                "total": sum(1 for it in items if it.get("type") == "item"),
                "SUCCESS": s,
                "FAILED": f,
                "summaryCallbacks": sum(1 for it in items if it.get("type") == "summary"),
            },
            "items": items,
        }

    @app.delete("/debug/plan-item-statuses")
    async def debug_clear_plan_item_statuses():
        with _debug_callback_lock:
            _debug_callback_store.clear()
        return {"ok": True, "message": "debug callback store cleared"}
