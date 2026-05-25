"""
API route handlers.
"""


from __future__ import annotations
import asyncio
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
import aiofiles

from .schemas import (
    UploadResponse,
    ExecuteStartRequest,
    ExecuteStartResponse,
    ExecutionStatus,
    SummaryResponse,
)

logger = logging.getLogger("bmc_auto_capture.api")

router = APIRouter()

# In-memory state for running executions (shared with the app)
_executions: dict[str, dict] = {}


def _get_app():
    """Lazy import to avoid circular deps."""
    from ..src.models.app_config import AppConfig
    from ..src.app import App
    return App


async def _run_execution(app, excel_path: str, execution_id: str):
    """Run the full pipeline in a background thread."""
    loop = asyncio.get_event_loop()

    def _run():
        try:
            _executions[execution_id]["phase"] = "running"
            results = app.run(excel_path)
            _executions[execution_id]["results"] = results
            _executions[execution_id]["phase"] = "complete"
        except Exception as e:
            logger.error("Execution %s failed: %s", execution_id, e)
            _executions[execution_id]["phase"] = "error"
            _executions[execution_id]["error"] = str(e)

    thread = threading.Thread(target=_run, daemon=True)
    _executions[execution_id]["thread"] = thread
    thread.start()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/config/upload-excel", response_model=UploadResponse)
async def upload_excel(file: UploadFile = File(...)):
    """Upload an Excel file, validate, return preview."""
    from ..src.loader.excel_reader import load_all
    from ..src.loader.schema_validator import validate

    # Save temp
    tmp_path = f"/tmp/{uuid.uuid4().hex}.xlsx"
    async with aiofiles.open(tmp_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    try:
        devices, tasks = load_all(tmp_path)
        report = validate(devices, tasks)

        return UploadResponse(
            status="ok" if report.is_valid else "error",
            device_count=report.device_count,
            device_enabled_count=report.device_enabled_count,
            task_count=report.task_count,
            task_enabled_count=report.task_enabled_count,
            errors=[
                {"level": m.level, "source": m.source, "row": m.row, "field": m.field, "message": m.message}
                for m in report.errors
            ],
            warnings=[
                {"level": m.level, "source": m.source, "row": m.row, "field": m.field, "message": m.message}
                for m in report.warnings
            ],
        )
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@router.post("/execute/start", response_model=ExecuteStartResponse)
async def execute_start(req: ExecuteStartRequest):
    """Start a new execution. Returns execution_id for tracking."""
    from ..src.models.app_config import AppConfig
    from ..src.app import App

    excel_path = req.excel_path
    if not os.path.exists(excel_path):
        raise HTTPException(status_code=404, detail=f"Excel file not found: {excel_path}")

    config = AppConfig()
    if req.config_path and os.path.exists(req.config_path):
        config = AppConfig.from_yaml(req.config_path)

    exec_id = uuid.uuid4().hex[:12]
    _executions[exec_id] = {
        "phase": "starting",
        "excel_path": excel_path,
        "config": config,
        "results": [],
        "thread": None,
    }

    app = App(config)

    # Wire event bus for progress tracking
    app.event_bus.subscribe("plan_completed", lambda **kw: _executions[exec_id].setdefault("completed", 0))
    # Count plans first
    from ..src.loader.excel_reader import load_all
    from ..src.scheduler.plan_generator import generate_plans
    devices, tasks = load_all(excel_path)
    plans = generate_plans(devices, tasks)

    _executions[exec_id]["plan_count"] = len(plans)
    _executions[exec_id]["completed_count"] = 0

    # Track completion via event bus
    def _on_complete(**kw):
        _executions[exec_id]["completed_count"] += 1

    app.event_bus.subscribe("plan_completed", _on_complete)

    await _run_execution(app, excel_path, exec_id)

    return ExecuteStartResponse(
        execution_id=exec_id,
        plan_count=len(plans),
        message=f"Execution started with {len(plans)} plans",
    )


@router.get("/execute/{exec_id}/status", response_model=ExecutionStatus)
async def get_status(exec_id: str):
    """Get current execution status."""
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    ex = _executions[exec_id]
    return ExecutionStatus(
        execution_id=exec_id,
        phase=ex.get("phase", "unknown"),
        completed=ex.get("completed_count", 0),
        total=ex.get("plan_count", 0),
        is_running=ex.get("phase") == "running",
        is_paused=False,
    )


@router.get("/execute/{exec_id}/stream")
async def stream_status(exec_id: str):
    """SSE stream of plan completion events."""
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    async def _stream():
        ex = _executions[exec_id]
        last_count = 0
        while ex.get("phase") in ("starting", "running"):
            current = ex.get("completed_count", 0)
            if current > last_count:
                yield f"data: {json.dumps({'completed': current, 'total': ex.get('plan_count', 0)})}\n\n"
                last_count = current
            await asyncio.sleep(1)
        yield f"data: {json.dumps({'completed': ex.get('completed_count', 0), 'total': ex.get('plan_count', 0), 'phase': ex.get('phase')})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/execute/{exec_id}/results")
async def get_results(exec_id: str):
    """Download merged result CSV."""
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    ex = _executions[exec_id]
    results = ex.get("results", [])
    if not results:
        raise HTTPException(status_code=404, detail="No results yet")

    import tempfile
    from ..src.output.collector import write_result_csv

    tmp = tempfile.mkdtemp()
    path = write_result_csv(results, tmp)

    return FileResponse(path, filename="result.csv", media_type="text/csv")


@router.get("/execute/{exec_id}/screenshots")
async def get_screenshots(
    exec_id: str,
    task_name: Optional[str] = Query(None),
    limit: int = Query(2),
):
    """Get screenshot paths, optionally filtered by task, limited to N devices."""
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    ex = _executions[exec_id]
    results = ex.get("results", [])

    # Filter by task name
    if task_name:
        results = [r for r in results if r.task_name == task_name]

    # Limit to N unique devices
    seen_devices: set[str] = set()
    filtered: list = []
    for r in results:
        if r.device_name not in seen_devices:
            seen_devices.add(r.device_name)
            filtered.append(r)
        if len(filtered) >= limit:
            break

    return {
        "screenshots": [
            {
                "plan_id": r.plan_id,
                "device_name": r.device_name,
                "task_name": r.task_name,
                "screenshot_paths": list(r.screenshots),
            }
            for r in filtered
            if r.screenshots
        ]
    }


@router.post("/execute/{exec_id}/stop")
async def stop_execution(exec_id: str):
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    _executions[exec_id]["phase"] = "stopped"
    return {"status": "stopped"}


@router.post("/execute/{exec_id}/pause")
async def pause_execution(exec_id: str):
    if exec_id not in _executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    _executions[exec_id]["phase"] = "paused"
    return {"status": "paused"}
