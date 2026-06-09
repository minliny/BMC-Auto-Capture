"""
PlanRunService — reads latest Excel, expands devices×tasks, executes with
FakeRunner or RealRunnerAdapter, sends per-item 6-field status callbacks.
"""

from __future__ import annotations
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..plan_item_status_callback_client import (
    PlanItemStatusCallbackClient,
    FakeCallbackTransport,
    HttpCallbackTransport,
)
from ..resource_lock_manager import ResourceLockManager

logger = logging.getLogger("bmc_auto_capture.plan_run")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class PlanRunItem:
    plan_id: int
    device_name: str
    task_name: str
    device_group: str = ""
    task_type: str = ""
    execution_mode: str = ""
    lock_uri: str = ""
    status: str = "PENDING"
    error_message: str | None = None
    # Device/task raw refs for real runner conversion
    _device: Any = None
    _task: Any = None


@dataclass
class PlanRun:
    plan_id: int
    run_id: str = ""
    status: str = "ACCEPTED"
    config_version: str = ""
    runner_mode: str = "fake"
    items: list[PlanRunItem] = field(default_factory=list)
    updater: str = "downstream-system"
    item_status_url: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.items),
            "success": sum(1 for i in self.items if i.status == "SUCCESS"),
            "failed": sum(1 for i in self.items if i.status == "FAILED"),
            "running": sum(1 for i in self.items if i.status == "RUNNING"),
            "pending": sum(1 for i in self.items if i.status == "PENDING"),
        }


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_excel_store: dict[str, Any] = {}
_store_lock = threading.Lock()


def _set_latest_excel(path: str) -> dict[str, Any]:
    from ..loader.excel_reader import load_all
    devices, tasks = load_all(path)
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        sha.update(f.read())
    config_version = f"excel-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
    enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]
    info = {
        "path": path, "sha256": sha.hexdigest(), "configVersion": config_version,
        "devices": devices, "tasks": tasks,
        "deviceCount": len(devices), "enabledDeviceCount": len(enabled_devices),
        "taskCount": len(tasks), "enabledTaskCount": len(enabled_tasks),
    }
    with _store_lock:
        _excel_store["latest"] = info
    return info


def _get_latest_excel() -> dict[str, Any] | None:
    with _store_lock:
        return _excel_store.get("latest")


# ---------------------------------------------------------------------------
# PlanRunService
# ---------------------------------------------------------------------------

class PlanRunService:
    """Orchestrates plan runs from latest Excel."""

    def __init__(self, use_http_callback: bool = False, callback_transport: Any = None,
                 lock_manager: ResourceLockManager | None = None):
        self._runs: dict[str, PlanRun] = {}
        self._runs_lock = threading.Lock()
        self._use_http = use_http_callback
        self._cb_transport = callback_transport
        self._lock_mgr = lock_manager or ResourceLockManager()

    @property
    def callback_transport(self):
        return self._cb_transport

    @property
    def lock_manager(self) -> ResourceLockManager:
        return self._lock_mgr

    # ------------------------------------------------------------------
    # Latest Excel config
    # ------------------------------------------------------------------

    def set_latest_excel(self, path: str) -> dict[str, Any]:
        try:
            info = _set_latest_excel(path)
        except Exception as e:
            return {"accepted": False, "reason": "INVALID_EXCEL_CONFIG", "message": str(e)}
        filename = os.path.basename(path)
        return {
            "accepted": True, "configVersion": info["configVersion"],
            "filename": filename, "sha256": info["sha256"],
            "deviceCount": info["deviceCount"], "enabledDeviceCount": info["enabledDeviceCount"],
            "taskCount": info["taskCount"], "enabledTaskCount": info["enabledTaskCount"],
            "message": "excel config accepted as latest",
        }

    # ------------------------------------------------------------------
    # Start plan run
    # ------------------------------------------------------------------

    def start_plan_run(self, plan_id: int, request: dict[str, Any]) -> dict[str, Any]:
        excel = _get_latest_excel()
        if excel is None:
            return {"accepted": False, "reason": "NO_LATEST_EXCEL_CONFIG"}

        callback = request.get("callback", {})
        item_status_url = callback.get("itemStatusUrl", "")
        updater = request.get("updater", "downstream-system")
        runner_mode = request.get("runner", "fake")

        if runner_mode not in ("fake", "real"):
            return {"accepted": False, "reason": f"INVALID_RUNNER: {runner_mode}"}

        run_id = f"plan-{plan_id}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        with self._runs_lock:
            for r in self._runs.values():
                if r.plan_id == plan_id and r.status == "RUNNING":
                    return {"accepted": False, "reason": "DUPLICATE",
                            "runId": r.run_id, "status": r.status}

        devices = excel["devices"]
        tasks = excel["tasks"]
        enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
        enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]

        items: list[PlanRunItem] = []
        for device in enabled_devices:
            for task in enabled_tasks:
                match_group = getattr(task, "match_group", "") or ""
                dg = getattr(device, "device_group", "") or ""
                if match_group:
                    # match_group supports "/"-separated groups, e.g. "L1/L2/A3"
                    allowed_groups = [g.strip().upper() for g in match_group.split("/") if g.strip()]
                    if dg.upper() not in allowed_groups:
                        continue
                lock_uri = _derive_lock_uri(device, task)
                items.append(PlanRunItem(
                    plan_id=plan_id,
                    device_name=getattr(device, "device_name", ""),
                    task_name=getattr(task, "task_name", ""),
                    device_group=dg,
                    task_type=getattr(task, "task_type", ""),
                    execution_mode=getattr(task, "execution_mode", ""),
                    lock_uri=lock_uri,
                    _device=device,
                    _task=task,
                ))

        run = PlanRun(
            plan_id=plan_id, run_id=run_id, status="RUNNING",
            runner_mode=runner_mode, config_version=excel["configVersion"],
            items=items, updater=updater, item_status_url=item_status_url,
            started_at=time.time(),
        )

        with self._runs_lock:
            self._runs[run_id] = run

        transport = self._cb_transport or (
            HttpCallbackTransport() if self._use_http else FakeCallbackTransport())
        cb = PlanItemStatusCallbackClient(transport=transport)

        t = threading.Thread(target=self._execute_run, args=(run, cb), daemon=True)
        t.start()

        return {
            "accepted": True, "planId": plan_id, "runId": run_id,
            "status": "ACCEPTED", "configVersion": excel["configVersion"],
            "message": "plan run accepted",
        }

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def _execute_run(self, run: PlanRun, cb: PlanItemStatusCallbackClient):
        is_real = run.runner_mode == "real"
        runner = None
        if is_real:
            from ..job_runner_adapter import RealRunnerAdapter
            runner = RealRunnerAdapter()

        for item in run.items:
            item.status = "RUNNING"

            # Lock
            if item.lock_uri:
                if not self._lock_mgr.acquire(item.lock_uri, f"{run.run_id}:{item.device_name}:{item.task_name}"):
                    item.status = "FAILED"
                    item.error_message = f"LOCK_CONFLICT: {item.lock_uri}"
                    self._do_callback(run, item, cb)
                    continue

            try:
                if is_real:
                    self._execute_real(item, runner)
                else:
                    self._execute_fake(item)
            finally:
                if item.lock_uri:
                    self._lock_mgr.release(item.lock_uri, f"{run.run_id}:{item.device_name}:{item.task_name}")

            self._do_callback(run, item, cb)

        run.finished_at = time.time()
        run.status = "COMPLETED"

    def _execute_fake(self, item: PlanRunItem):
        time.sleep(0.001)
        item.status = "SUCCESS"
        item.error_message = None

    def _execute_real(self, item: PlanRunItem, runner: Any):
        task_type = item.task_type.upper()
        exec_mode = item.execution_mode.upper()

        if task_type not in ("BMC", "SSH") and exec_mode not in ("BMC_URL", "BMC_ACTIONS", "SSH_CMD"):
            item.status = "FAILED"
            item.error_message = f"UNSUPPORTED_TASK_TYPE: {task_type}/{exec_mode}"
            return

        try:
            job_payload = self._build_job_payload(item)
        except Exception as e:
            item.status = "FAILED"
            item.error_message = f"PAYLOAD_BUILD_FAILED: {e}"
            return

        try:
            result = runner.run_job(job_payload)
        except Exception as e:
            item.status = "FAILED"
            item.error_message = f"RUNNER_CRASH: {e}"
            return

        if result.status == "SUCCEEDED":
            item.status = "SUCCESS"
            item.error_message = None
        elif result.status == "TIMEOUT":
            item.status = "FAILED"
            item.error_message = f"TIMEOUT: {result.error.get('message', '') if result.error else 'timeout'}"
        else:
            item.status = "FAILED"
            item.error_message = result.error.get("message", "FAILED") if result.error else "FAILED"

    def _build_job_payload(self, item: PlanRunItem) -> dict[str, Any]:
        device = item._device
        task = item._task

        if device is None or task is None:
            raise ValueError("Missing device or task reference")

        bmc_ip = (getattr(device, "bmc_ip", "") or "").strip()
        inband_ip = (getattr(device, "inband_ip", "") or "").strip()
        task_type = getattr(task, "task_type", "")
        exec_mode = getattr(task, "execution_mode", "")
        cmd = getattr(task, "command_or_url", "") or ""

        # Check for per_group_commands override
        try:
            pgc = getattr(task, '_per_group_commands', None) or {}
            if pgc and item.device_group.upper() in pgc:
                cmd = pgc[item.device_group.upper()]
        except Exception:
            pass

        # Derive ssh_type for secrets
        dg = (getattr(device, "device_group", "") or "").upper()
        st = "SSH_VRP" if dg in ("L1", "L2") else "SSH"

        # secret_ref from device — never log plaintext
        bmc_user = getattr(device, "bmc_username", "") or ""
        bmc_pass = getattr(device, "bmc_password", "") or ""
        ssh_user = getattr(device, "inband_username", "") or ""
        ssh_pass = getattr(device, "inband_password", "") or ""

        oob_ref = bmc_pass if bmc_pass.startswith("env:") or bmc_pass.startswith("secret:") else ""
        inband_ref = ssh_pass if ssh_pass.startswith("env:") or ssh_pass.startswith("secret:") else ""

        # If password is plaintext (from Excel), wrap as plain: so secret_resolver handles it
        if bmc_pass and not oob_ref:
            oob_ref = bmc_pass
        if ssh_pass and not inband_ref:
            inband_ref = ssh_pass

        device_snapshot = {
            "device_name": item.device_name,
            "device_group": item.device_group,
            "oob_ip": bmc_ip, "oob_port": 443,
            "oob_username": bmc_user, "oob_password_ref": oob_ref,
            "inband_ip": inband_ip, "inband_port": 22,
            "inband_username": ssh_user, "inband_password_ref": inband_ref,
            "ssh_type": st,
        }

        task_snapshot = {
            "task_name": item.task_name,
            "task_type": task_type,
            "execution_mode": exec_mode,
            "url": cmd,
            "command_or_url": cmd,
            "ssh_cmd": cmd,
            "timeout_seconds": int(getattr(task, "timeout_seconds", 60) or 60),
            "retry_count": 0,
        }

        return {
            "device_snapshot": device_snapshot,
            "task_snapshot": task_snapshot,
        }

    def _do_callback(self, run: PlanRun, item: PlanRunItem, cb: PlanItemStatusCallbackClient):
        if not run.item_status_url:
            return
        cb.send(
            url=run.item_status_url, plan_id=item.plan_id,
            device_name=item.device_name, task_name=item.task_name,
            status=item.status, updater=run.updater,
            error_message=item.error_message,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return {"planId": run.plan_id, "runId": run.run_id,
                "status": run.status, "summary": run.summary}

    def get_run_items(self, run_id: str) -> dict[str, Any] | None:
        """Get run with per-item details. Returns None if run not found."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        items = [
            {
                "deviceName": item.device_name,
                "taskName": item.task_name,
                "status": item.status,
                "errorMessage": item.error_message,
            }
            for item in run.items
        ]
        return {
            "planId": run.plan_id,
            "runId": run.run_id,
            "status": run.status,
            "summary": run.summary,
            "items": items,
        }

    def run_all_sync(self, run_id: str):
        """Execute run synchronously (for testing).
        If run is already running (background thread), wait for it.
        """
        run = self._runs.get(run_id)
        if run is None:
            return
        if run.status == "COMPLETED":
            return
        transport = self._cb_transport or FakeCallbackTransport()
        cb = PlanItemStatusCallbackClient(transport=transport)
        self._execute_run(run, cb)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_lock_uri(device: Any, task: Any) -> str:
    bmc_ip = (getattr(device, "bmc_ip", "") or "").strip()
    inband_ip = (getattr(device, "inband_ip", "") or "").strip()
    tt = (getattr(task, "task_type", "") or "").upper()
    em = (getattr(task, "execution_mode", "") or "").upper()
    if tt in ("BMC",) or em in ("BMC_URL", "BMC_ACTIONS"):
        return f"bmc://{bmc_ip}" if bmc_ip else ""
    if tt in ("SSH", "TELNET") or em in ("SSH_CMD",):
        if not inband_ip:
            return ""
        dg = (getattr(device, "device_group", "") or "").upper()
        if dg in ("L1", "L2"):
            return f"ssh-vrp://{inband_ip}"
        return f"ssh://{inband_ip}"
    return ""
