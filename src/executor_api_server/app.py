"""
FastAPI application for the Executor API v1.
"""

from __future__ import annotations
import logging
import os
import socket
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from src._version import APP_VERSION
from .schemas import (
    ExecutorStatusResponse, PlanRunRequest, ExternalPlanRequest,
    ExcelPathRequest, ExcelConfigAcceptResponse, LatestConfigResponse,
    PlanRunAcceptResponse, PlanStatusResponse, PlanItemsResponse,
    CallbackRetryRequest, CallbackRetryResponse,
    RulePackValidationResponse, RulePackImportResponse,
)
from .status_service import ExecutorRuntimeStatusService
from .contracts import get_contract_index, get_contract

logger = logging.getLogger("bmc_auto_capture.executor_api")

# Shared store for debug callback receiver (thread-safe, in-memory)
_debug_callback_store: list[dict] = []
_debug_callback_lock = threading.Lock()

# Excel config store: writes to executor_state/configs/by_hash/ + latest.json.
# The store can migrate historical .runtime/configs/latest.xlsx files.
_config_store: object | None = None  # ExcelConfigStore, lazy init


def create_app(
    status_service: ExecutorRuntimeStatusService | None = None,
    plan_run_service=None,  # PlanRunService, optional
    debug_callback_receiver: bool = False,  # Enable built-in debug callback receiver
) -> FastAPI:
    status_service = status_service or ExecutorRuntimeStatusService()
    app = FastAPI(title="BMC Auto-Capture Executor API v0.2", version=APP_VERSION)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # Convert Pydantic validation errors to the API's stable 400 body.
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
    # Core utility routes
    # ==================================================================
    _register_utility_routes(app)

    # ==================================================================
    # Contract / schema query (read-only, no side effects)
    # ==================================================================
    _register_contract_routes(app)

    # ==================================================================
    # RulePack config API
    # ==================================================================
    _register_rule_pack_routes(app)

    # ==================================================================
    # Debug callback receiver (方案 B — built-in, no external Python needed)
    # ==================================================================

    if debug_callback_receiver:
        _register_debug_callback_receiver(app)

    @app.get("/executor/v1/status", response_model=ExecutorStatusResponse)
    async def get_status():
        return ExecutorStatusResponse(**status_service.get_executor_status())

    # ==================================================================
    # Plan Run Item Status Callback
    # ==================================================================

    if plan_run_service is not None:
        _register_plan_run_routes(app, plan_run_service)

    return app


# ==================================================================
# Core utility routes
# ==================================================================

def _register_utility_routes(app: FastAPI):
    """Register /version, /network/ping, and /routes.

    Note: /health is registered in create_app() to access plan_run_service.
    """

    @app.get("/version")
    async def version():
        info = {
            "name": "bmc-auto-capture",
            "mode": "executor-api",
            "version": APP_VERSION,
            "status": "ok",
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


# ==================================================================
# RulePack config routes
# ==================================================================

def _rule_pack_from_body(body):
    if isinstance(body, dict) and isinstance(body.get("rulePack"), dict):
        return body["rulePack"]
    return body


def _rule_packs_from_body(body):
    if isinstance(body, dict) and isinstance(body.get("rulePacks"), list):
        return body["rulePacks"]
    if isinstance(body, dict) and isinstance(body.get("rulePack"), dict):
        return [body["rulePack"]]
    if isinstance(body, list):
        return body
    return [body]


def _register_rule_pack_routes(app: FastAPI):
    """Register RulePack capability, validation, import, and update routes."""
    from ..rulepacks import RulePackStore, get_rule_capabilities, validate_rule_pack

    @app.get("/executor/v1/config/rule-capabilities")
    async def get_rule_capabilities_route():
        return get_rule_capabilities()

    @app.post("/executor/v1/config/rule-packs:validate", response_model=RulePackValidationResponse)
    async def validate_rule_pack_route(request: Request):
        body = await request.json()
        report = validate_rule_pack(_rule_pack_from_body(body))
        return report.to_dict(include_normalized=True)

    @app.post("/executor/v1/config/rule-packs:import", response_model=RulePackImportResponse)
    async def import_rule_packs_route(request: Request):
        body = await request.json()
        store = RulePackStore()
        items = []
        errors = []
        for raw_pack in _rule_packs_from_body(body):
            report = validate_rule_pack(raw_pack)
            if not report.valid:
                errors.append(report.to_dict(include_normalized=True))
                continue
            try:
                saved = store.put(report.normalized)
                items.append({
                    "taskId": saved["taskId"],
                    "protocol": saved["protocol"],
                    "path": saved["path"],
                })
            except Exception as exc:
                errors.append({
                    "valid": False,
                    "errors": [{"code": "RULEPACK_IMPORT_FAILED", "message": str(exc)}],
                    "warnings": [],
                })
        accepted = not errors
        status_code = 200 if accepted else 400
        return JSONResponse(
            status_code=status_code,
            content={
                "accepted": accepted,
                "imported": len(items),
                "failed": len(errors),
                "items": items,
                "errors": errors,
            },
        )

    @app.get("/executor/v1/config/rule-packs")
    async def list_rule_packs_route():
        return {"items": RulePackStore().list()}

    @app.get("/executor/v1/config/rule-packs/{task_id}")
    async def get_rule_pack_route(task_id: str):
        pack = RulePackStore().get(task_id)
        if pack is None:
            raise HTTPException(status_code=404, detail={
                "code": "RULEPACK_NOT_FOUND",
                "message": f"RulePack not found: {task_id}",
            })
        return pack

    @app.put("/executor/v1/config/rule-packs/{task_id}", response_model=RulePackImportResponse)
    async def put_rule_pack_route(task_id: str, request: Request):
        body = await request.json()
        pack = _rule_pack_from_body(body)
        if not isinstance(pack, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "imported": 0,
                    "failed": 1,
                    "items": [],
                    "errors": [{"code": "RULEPACK_NOT_OBJECT", "message": "RulePack must be an object"}],
                },
            )
        if str(pack.get("task_id") or "") != task_id:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "imported": 0,
                    "failed": 1,
                    "items": [],
                    "errors": [{"code": "RULEPACK_TASK_ID_MISMATCH", "message": "path task_id and body task_id differ"}],
                },
            )
        report = validate_rule_pack(pack)
        if not report.valid:
            return JSONResponse(
                status_code=400,
                content={
                    "accepted": False,
                    "imported": 0,
                    "failed": 1,
                    "items": [],
                    "errors": [report.to_dict(include_normalized=True)],
                },
            )
        saved = RulePackStore().put(report.normalized)
        return {
            "accepted": True,
            "imported": 1,
            "failed": 0,
            "items": [{
                "taskId": saved["taskId"],
                "protocol": saved["protocol"],
                "path": saved["path"],
            }],
            "errors": [],
        }


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
):
    """Plan run routes: Excel config, run start, query, and remote upload."""

    @app.post("/executor/v1/config/excel:path", response_model=ExcelConfigAcceptResponse)
    async def set_latest_excel_path(req: ExcelPathRequest):
        """Set latest Excel by executor-local path (local debug only).

        Uses ExcelConfigStore.activate_from_local_path() which copies the
        file into executor_state/configs/by_hash/ and writes latest.json.
        Historical .runtime/configs/latest.xlsx files are migrated by
        ExcelConfigStore when present.
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
        Falls back to process-local state created by set_latest_excel().
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
          - numeric local plan IDs: no query params needed
          - external plan IDs: requires excelHash query param
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
