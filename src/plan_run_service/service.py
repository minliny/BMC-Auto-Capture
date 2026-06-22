"""
PlanRunService — reads latest Excel, expands devices×tasks, executes with
FakeRunner or RealRunnerAdapter, sends planId-keyed plan-item status callbacks.
"""

from __future__ import annotations
import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .callback_delivery import CallbackDeliveryService
from .job_payload import PlanRunJobPayloadBuilder
from .models import PlanRun, PlanRunItem, RunConfigSnapshot
from ..plan_item_status_callback_client import (
    PlanItemStatusCallbackClient,
    FakeCallbackTransport,
    HttpCallbackTransport,
)
from .builder import PlanRunBuilder
from .query_projector import PlanRunQueryProjector
from .result_reports import PlanRunResultReporter
from .state_codec import PlanRunStateCodec
from .state_store import PlanRunStateStore
from ..resource_lock_manager import ResourceLockManager
from ..utils.sensitive import redact_sensitive_text, redact_url_for_log

logger = logging.getLogger("bmc_auto_capture.plan_run")


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
    enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
    enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]
    info = {
        "path": path, "sha256": sha.hexdigest(),
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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# PlanRunService
# ---------------------------------------------------------------------------

class PlanRunService:
    """Orchestrates plan runs from latest Excel."""

    def __init__(self, use_http_callback: bool = False, callback_transport: Any = None,
                 lock_manager: ResourceLockManager | None = None,
                 workspace_root: str | None = None,
                 allow_real_runner: bool = False,
                 result_writer: Any = None,
                 result_reporter: Any = None,
                 state_store: PlanRunStateStore | None = None,
                 query_projector: Any = None,
                 plan_builder: Any = None,
                 callback_delivery: Any = None,
                 state_codec: Any = None,
                 job_payload_builder: Any = None):
        if workspace_root is None:
            from ..excel_config_store import _resolve_workspace
            workspace_root = str(_resolve_workspace())
        self._runs: dict[str, PlanRun] = {}
        self._runs_by_run_id: dict[str, PlanRun] = {}
        self._run_threads: dict[str, threading.Thread] = {}
        self._runs_lock = threading.Lock()
        self._cb_transport = callback_transport
        self._lock_mgr = lock_manager or ResourceLockManager()
        self._workspace_root = workspace_root
        self._allow_real_runner = allow_real_runner or _env_truthy("EXECUTOR_ENABLE_REAL_RUNNER")
        self._result_reporter = result_reporter or PlanRunResultReporter(result_writer=result_writer)
        self._state_store = state_store or PlanRunStateStore(self._workspace_root)
        self._query_projector = query_projector or PlanRunQueryProjector()
        self._plan_builder = plan_builder or PlanRunBuilder()
        self._callback_delivery = (
            callback_delivery or CallbackDeliveryService(self._workspace_root)
        )
        self._state_codec = state_codec or PlanRunStateCodec()
        self._job_payload_builder = job_payload_builder or PlanRunJobPayloadBuilder()
        self._load_persisted_runs()

        # P1-4: use_http_callback is retained as a constructor alias. Transport
        # selection now uses _resolve_transport(), which auto-detects
        # HttpCallbackTransport when itemStatusUrl is provided.
        if use_http_callback:
            logger.warning(
                "PlanRunService(use_http_callback=True) is deprecated. "
                "Transport is now auto-selected via _resolve_transport() based on "
                "itemStatusUrl. Pass callback_transport=HttpCallbackTransport() "
                "if you need an explicit HTTP transport."
            )
        self._use_http = use_http_callback  # accepted constructor alias

    @property
    def callback_transport(self):
        return self._cb_transport

    @property
    def lock_manager(self) -> ResourceLockManager:
        return self._lock_mgr

    @property
    def allow_real_runner(self) -> bool:
        return self._allow_real_runner

    def _validate_runner_mode(self, runner_mode: str) -> dict[str, Any]:
        if runner_mode not in ("fake", "real"):
            return {"ok": False, "reason": f"INVALID_RUNNER: {runner_mode}"}
        if runner_mode == "real" and not self._allow_real_runner:
            return {
                "ok": False,
                "reason": "REAL_RUNNER_NOT_ENABLED",
                "message": (
                    "runner=real requires server-side enablement via "
                    "PlanRunService(allow_real_runner=True) or "
                    "EXECUTOR_ENABLE_REAL_RUNNER=1"
                ),
            }
        return {"ok": True}

    @staticmethod
    def _validate_callback_url(url: str) -> dict[str, Any]:
        return CallbackDeliveryService.validate_callback_url(url)

    # ------------------------------------------------------------------
    # Transport resolution: auto HTTP when itemStatusUrl is provided
    # ------------------------------------------------------------------

    def _resolve_transport(self, item_status_url: str):
        """Resolve callback transport.

        Priority:
        1. Explicit transport set at construction time (self._cb_transport)
        2. HttpCallbackTransport if itemStatusUrl is non-empty
        3. FakeCallbackTransport as fallback
        """
        if self._cb_transport is not None:
            return self._cb_transport
        if item_status_url:
            return HttpCallbackTransport()
        return FakeCallbackTransport()

    def _make_plan_output_root(self, plan_id: int | str) -> str:
        """Create a stable per-plan output root for API-triggered execution."""
        return self._state_store.make_plan_output_root(plan_id)

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
            "accepted": True,
            "excelHash": info["sha256"],
            "filename": filename, "sha256": info["sha256"],
            "deviceCount": info["deviceCount"], "enabledDeviceCount": info["enabledDeviceCount"],
            "taskCount": info["taskCount"], "enabledTaskCount": info["enabledTaskCount"],
            "message": "excel config accepted as latest",
        }

    # ------------------------------------------------------------------
    # External Plan API (excelHash + string planId)
    # ------------------------------------------------------------------

    def start_external_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start external plan via excelHash + server planId.

        The planId from callback.planId is the single business plan ID.
        No runId, executionKey, or localPlanId is used.

        ISSUE-001: validates + snapshots latest config at entry.
        Changing latest.json after this does NOT affect the run.
        """
        excel_hash = request.get("excelHash", "")
        if not excel_hash:
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": "MISSING_EXCEL_HASH"}

        # ISSUE-001: validate + snapshot at :run entry
        snap_result = self._validate_and_snapshot_latest()
        if not snap_result.get("ok"):
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": snap_result.get("reason", "UNKNOWN"),
                    "message": snap_result.get("message", "")}

        snapshot: RunConfigSnapshot = snap_result["snapshot"]
        devices = snap_result["devices"]
        tasks = snap_result["tasks"]

        # Validate caller-provided excelHash matches snapshot
        if excel_hash != snapshot.excel_hash:
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": "EXCEL_HASH_MISMATCH"}

        callback = request.get("callback", {})
        item_status_url = callback.get("itemStatusUrl", "")
        callback_mode = callback.get("mode", "batch")
        callback_url_check = self._validate_callback_url(item_status_url)
        if not callback_url_check.get("ok"):
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": callback_url_check.get("reason", "INVALID_CALLBACK_URL"),
                    "message": callback_url_check.get("message", "")}
        # planId from callback.planId IS the single business plan ID
        plan_id = str(callback.get("planId", ""))
        if not plan_id:
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": "MISSING_CALLBACK_PLAN_ID"}
        if callback_mode not in ("batch", "single"):
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": f"INVALID_CALLBACK_MODE: {callback_mode}"}
        updater = request.get("updater", "downstream-system")
        runner_mode = request.get("runner", "fake")
        runner_check = self._validate_runner_mode(runner_mode)
        if not runner_check.get("ok"):
            return {"accepted": False, "status": "FAILED",
                    "errorMessage": runner_check.get("reason", "INVALID_RUNNER"),
                    "message": runner_check.get("message", "")}

        # Check for duplicate — planId already running
        with self._runs_lock:
            existing = self._runs.get(str(plan_id))
            if existing and existing.status == "RUNNING":
                return {"accepted": False, "status": "PLAN_ALREADY_RUNNING",
                        "errorMessage": f"Plan {plan_id} is already running"}

        items = self._plan_builder.build_items(
            plan_id,
            devices,
            tasks,
            item_factory=PlanRunItem,
        )

        run_id = f"plan-{plan_id}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        output_root = self._make_plan_output_root(plan_id)

        run = PlanRun(
            plan_id=plan_id, run_id=run_id, excel_hash=snapshot.excel_hash,
            status="RUNNING", runner_mode=runner_mode, output_root=output_root,
            items=items,
            updater=updater, item_status_url=item_status_url,
            callback_mode=callback_mode,
            started_at=time.time(),
            config_snapshot=snapshot,
        )

        with self._runs_lock:
            self._runs[str(plan_id)] = run
            self._runs_by_run_id[run_id] = run
        self._persist_run(run)

        transport = self._resolve_transport(item_status_url)
        cb = PlanItemStatusCallbackClient(transport=transport)

        # Log callback configuration
        if item_status_url:
            transport_mode = "http" if isinstance(transport, HttpCallbackTransport) else "fake"
            logger.info(
                "callback configured: url=%s mode=%s transport=%s planId=%s internalRunId=%s itemCount=%d",
                redact_url_for_log(item_status_url), callback_mode, transport_mode,
                plan_id, run_id, len(items),
            )
        else:
            logger.info("callback not configured: no itemStatusUrl provided")

        t = threading.Thread(target=self._execute_run, args=(run, cb), daemon=True)
        with self._runs_lock:
            self._run_threads[str(plan_id)] = t
        t.start()

        return {
            "accepted": True, "excelHash": excel_hash,
            "planId": plan_id, "status": "ACCEPTED",
        }

    def get_external_plan(self, plan_id: str, excel_hash: str) -> dict[str, Any] | None:
        """Get external plan summary. Validates excelHash. Returns None if not found."""
        if not excel_hash:
            return None
        run = self._get_plan(plan_id)
        if run is None:
            return None
        if run.excel_hash != excel_hash:
            return None
        return self._public_plan(run, include_items=False)

    def get_external_plan_items(self, plan_id: str, excel_hash: str) -> dict[str, Any] | None:
        """Get external plan items. Validates excelHash. Returns None if not found."""
        if not excel_hash:
            return None
        run = self._get_plan(plan_id)
        if run is None:
            return None
        if run.excel_hash != excel_hash:
            return None
        return self._public_plan(run, include_items=True)

    def _get_plan(self, plan_id: str) -> PlanRun | None:
        """Find plan by plan_id (direct key lookup, O(1))."""
        with self._runs_lock:
            return self._runs.get(str(plan_id))

    def _get_run_by_id(self, run_id: str) -> PlanRun | None:
        with self._runs_lock:
            return self._runs_by_run_id.get(str(run_id))

    def _public_item(self, item: PlanRunItem) -> dict[str, Any]:
        return self._query_projector.item(item)

    def _public_plan(self, run: PlanRun, include_items: bool = False) -> dict[str, Any]:
        return self._query_projector.plan(run, include_items=include_items)

    def _run_to_state(self, run: PlanRun) -> dict[str, Any]:
        return self._state_codec.run_to_state(run)

    def _state_to_run(self, data: dict[str, Any]) -> PlanRun | None:
        return self._state_codec.state_to_run(data)

    def _persist_run(self, run: PlanRun) -> None:
        if not run.run_id:
            return
        try:
            self._state_store.persist_run_state(run.run_id, run.plan_id, self._run_to_state(run))
        except Exception as exc:
            logger.warning("PlanRun state persist failed: %s", redact_sensitive_text(str(exc)))

    def _load_persisted_runs(self) -> None:
        loaded = 0
        for path_name, data in self._state_store.load_run_states():
            try:
                run = self._state_to_run(data)
                if run is None:
                    continue
                self._runs_by_run_id[run.run_id] = run
                current = self._runs.get(str(run.plan_id))
                if current is None or run.started_at >= current.started_at:
                    self._runs[str(run.plan_id)] = run
                loaded += 1
            except Exception as exc:
                logger.warning("PlanRun state load skipped for %s: %s", path_name, redact_sensitive_text(str(exc)))
        if loaded:
            logger.info("PlanRun state restored: %d run(s)", loaded)

    # ------------------------------------------------------------------
    # Config snapshot validation (ISSUE-001)
    # ------------------------------------------------------------------

    def _validate_and_snapshot_latest(self) -> dict[str, Any]:
        """Read latest config, validate integrity, return snapshot or error.

        Returns dict with keys:
          - ok=True + snapshot=RunConfigSnapshot + devices + tasks   (success)
          - ok=False + reason + message                              (failure)

        Once a snapshot is created, changing latest.json does NOT affect it.
        """
        # 1. Try ExcelConfigStore latest.json first
        try:
            from ..excel_config_store import get_default_store
            store = get_default_store()
            meta = store.get_latest()
        except Exception:
            meta = None

        # NEW-005: Check get_latest() error codes (CONFIG_CORRUPTED, LATEST_EXCEL_MISSING, HASH mismatch, etc.)
        if isinstance(meta, dict) and meta.get("code"):
            reason = meta["code"]
            msg = "Latest Excel config is unavailable or inconsistent"
            return {"ok": False, "reason": reason, "message": msg}

        # 2. Fallback to in-memory _get_latest_excel()
        #    Only when store.get_latest() returned None (no json at all).
        #    Damaged latest.json never falls back.
        excel = _get_latest_excel()
        if meta is None and excel is None:
            return {"ok": False, "reason": "NO_LATEST_EXCEL_CONFIG",
                    "message": "No latest Excel config available"}

        # If we have store meta, use it; otherwise build from in-memory
        if meta:
            stored_path = meta.get("storedPath", "")
            excel_hash = meta.get("excelHash", "")

            # Validate storedPath exists
            if not stored_path or not os.path.isfile(stored_path):
                return {"ok": False, "reason": "LATEST_EXCEL_MISSING",
                        "message": "Latest Excel config file is missing"}

            # Validate sha256 matches
            try:
                actual_sha = hashlib.sha256()
                with open(stored_path, "rb") as f:
                    actual_sha.update(f.read())
                actual_hash = actual_sha.hexdigest()
                if actual_hash != excel_hash:
                    return {"ok": False, "reason": "LATEST_EXCEL_HASH_MISMATCH",
                            "message": "Latest Excel config hash mismatch"}
            except OSError as e:
                return {"ok": False, "reason": "LATEST_EXCEL_READ_ERROR",
                        "message": "Cannot read latest Excel config for hash validation"}

            snapshot = RunConfigSnapshot.from_latest_meta(meta)

            # NEW-004: ALWAYS load devices/tasks from storedPath, never from
            # in-memory excel.  This guarantees snapshot metadata and
            # devices/tasks originate from the exact same file.
            try:
                from ..loader.excel_reader import load_all
                devices, tasks = load_all(stored_path)
                excel = {
                    "devices": devices, "tasks": tasks,
                    "path": stored_path, "sha256": excel_hash,
                    "deviceCount": snapshot.device_count,
                    "enabledDeviceCount": snapshot.enabled_device_count,
                    "taskCount": snapshot.task_count,
                    "enabledTaskCount": snapshot.enabled_task_count,
                }
            except Exception as e:
                return {"ok": False, "reason": "LATEST_EXCEL_PARSE_ERROR",
                        "message": "Cannot parse latest Excel config"}

            return {"ok": True, "snapshot": snapshot, "devices": excel["devices"],
                    "tasks": excel["tasks"], "excel": excel}

        # Process-local in-memory-only path
        if excel is None:
            return {"ok": False, "reason": "NO_LATEST_EXCEL_CONFIG",
                    "message": "No latest Excel config available"}

        stored_path = excel.get("path", "")
        excel_hash = excel.get("sha256", "")

        # Validate file exists
        if not stored_path or not os.path.isfile(stored_path):
            return {"ok": False, "reason": "LATEST_EXCEL_MISSING",
                    "message": "Excel file is missing"}

        # Build a minimal snapshot from in-memory state
        snapshot = RunConfigSnapshot(
            excel_hash=excel_hash,
            stored_path=stored_path,
            original_filename=os.path.basename(stored_path),
            source="in_memory",
            device_count=excel.get("deviceCount", 0),
            enabled_device_count=excel.get("enabledDeviceCount", 0),
            task_count=excel.get("taskCount", 0),
            enabled_task_count=excel.get("enabledTaskCount", 0),
        )

        return {"ok": True, "snapshot": snapshot, "devices": excel["devices"],
                "tasks": excel["tasks"], "excel": excel}

    # ------------------------------------------------------------------
    # Start local plan run
    # ------------------------------------------------------------------

    def start_plan_run(self, plan_id: int, request: dict[str, Any]) -> dict[str, Any]:
        # ISSUE-001: validate + snapshot at :run entry — never re-read latest
        snap_result = self._validate_and_snapshot_latest()
        if not snap_result.get("ok"):
            return {"accepted": False,
                    "reason": snap_result.get("reason", "UNKNOWN"),
                    "message": snap_result.get("message", "")}

        snapshot: RunConfigSnapshot = snap_result["snapshot"]
        devices = snap_result["devices"]
        tasks = snap_result["tasks"]

        callback = request.get("callback", {})
        item_status_url = callback.get("itemStatusUrl", "")
        callback_mode = callback.get("mode", "batch")
        callback_url_check = self._validate_callback_url(item_status_url)
        if not callback_url_check.get("ok"):
            return {"accepted": False,
                    "reason": callback_url_check.get("reason", "INVALID_CALLBACK_URL"),
                    "message": callback_url_check.get("message", "")}
        if callback_mode not in ("batch", "single"):
            return {"accepted": False, "reason": f"INVALID_CALLBACK_MODE: {callback_mode}"}
        updater = request.get("updater", "downstream-system")
        runner_mode = request.get("runner", "fake")
        runner_check = self._validate_runner_mode(runner_mode)
        if not runner_check.get("ok"):
            return {"accepted": False,
                    "reason": runner_check.get("reason", "INVALID_RUNNER"),
                    "message": runner_check.get("message", "")}

        # P1-1: path plan_id is authoritative.  If callback.planId is provided
        # and differs from path plan_id, warn but do NOT change run ownership.
        callback_plan_id = callback.get("planId", "")
        if callback_plan_id and str(callback_plan_id) != str(plan_id):
            logger.warning(
                "callback.planId (%s) differs from path plan_id (%s) — "
                "path plan_id is authoritative, callback.planId ignored",
                callback_plan_id, plan_id,
            )

        # Check for duplicate — plan_id already running
        with self._runs_lock:
            existing = self._runs.get(str(plan_id))
            if existing and existing.status == "RUNNING":
                return {"accepted": False, "reason": "PLAN_ALREADY_RUNNING",
                        "planId": plan_id, "status": existing.status}

        items = self._plan_builder.build_items(
            plan_id,
            devices,
            tasks,
            item_factory=PlanRunItem,
        )

        run_id = f"plan-{plan_id}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        output_root = self._make_plan_output_root(plan_id)

        run = PlanRun(
            plan_id=plan_id, run_id=run_id, status="RUNNING",
            runner_mode=runner_mode, output_root=output_root,
            items=items, updater=updater, item_status_url=item_status_url,
            callback_mode=callback_mode,
            started_at=time.time(),
            excel_hash=snapshot.excel_hash,
            config_snapshot=snapshot,
        )

        with self._runs_lock:
            self._runs[str(plan_id)] = run
            self._runs_by_run_id[run_id] = run
        self._persist_run(run)

        transport = self._resolve_transport(item_status_url)
        cb = PlanItemStatusCallbackClient(transport=transport)

        # Log callback configuration
        if item_status_url:
            transport_mode = "http" if isinstance(transport, HttpCallbackTransport) else "fake"
            logger.info(
                "callback configured: url=%s mode=%s transport=%s planId=%s internalRunId=%s itemCount=%d",
                redact_url_for_log(item_status_url), callback_mode, transport_mode,
                plan_id, run_id, len(items),
            )
        else:
            logger.info("callback not configured: no itemStatusUrl provided")

        t = threading.Thread(target=self._execute_run, args=(run, cb), daemon=True)
        with self._runs_lock:
            self._run_threads[str(plan_id)] = t
        t.start()

        return {
            "accepted": True, "planId": plan_id,
            "status": "ACCEPTED",
            "excelHash": snapshot.excel_hash,
            "message": "plan run accepted",
        }

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def _resolve_callback_url(self, run: PlanRun) -> str:
        return self._callback_delivery.resolve_callback_url(run)

    def _execute_run(self, run: PlanRun, cb: PlanItemStatusCallbackClient):
        is_real = run.runner_mode == "real"
        runner = None
        if is_real:
            from ..job_runner_adapter import RealRunnerAdapter
            runner = RealRunnerAdapter(output_root=run.output_root or "./output_api_direct")

        # Execute in plan order.  Consecutive same-endpoint BMC items can share
        # one login/session; all other items use the normal per-item path.
        item_index = 0
        while item_index < len(run.items):
            item = run.items[item_index]
            if is_real and self._is_bmc_item(item):
                group = self._collect_bmc_session_group(run.items, item_index)
                if len(group) > 1:
                    self._execute_real_bmc_group(run, group, runner, cb)
                    item_index += len(group)
                    continue

            self._execute_run_item(run, item, cb, is_real=is_real, runner=runner)
            item_index += 1

        run.finished_at = time.time()
        run.status = "COMPLETED"
        self._write_plan_result_reports(run)
        self._persist_run(run)
        self._deliver_plan_summary(run, cb)

    def _execute_run_item(
        self, run: PlanRun, item: PlanRunItem, cb: PlanItemStatusCallbackClient,
        is_real: bool, runner: Any,
    ) -> None:
        self._mark_item_started(run, item, cb)
        owner = self._lock_owner(run, item)

        if item.lock_uri:
            if not self._lock_mgr.acquire(item.lock_uri, owner):
                item.status = "FAILED"
                item.error_message = f"LOCK_CONFLICT: {item.lock_uri}"
                item.add_info_event("ERROR", f"Lock conflict: {item.lock_uri}")
                self._mark_item_finished(run, item, cb)
                return

        try:
            if is_real:
                self._execute_real(item, runner)
            else:
                self._execute_fake(item)
        finally:
            if item.lock_uri:
                self._lock_mgr.release(item.lock_uri, owner)

        self._mark_item_finished(run, item, cb)

    def _mark_item_started(
        self, run: PlanRun, item: PlanRunItem, cb: PlanItemStatusCallbackClient,
    ) -> None:
        item.status = "IN_PROGRESS"
        item.started_at = time.time()
        item.add_info_event(
            "INFO",
            f"PlanItem started: planItemId={item.plan_item_id} taskId={item.task_id} "
            f"device={item.device_name} task={item.task_name}",
        )
        self._persist_run(run)
        self._deliver_item_status(run, item, cb)

    def _mark_item_finished(
        self, run: PlanRun, item: PlanRunItem, cb: PlanItemStatusCallbackClient,
    ) -> None:
        item.finished_at = time.time()
        if item.status == "SUCCESS":
            item.add_info_event("INFO", f"PlanItem completed: status={item.status}")
        else:
            item.add_info_event("ERROR", f"PlanItem failed: status={item.status} error={item.error_message}")
        self._persist_run(run)
        self._deliver_item_status(run, item, cb)

    def _lock_owner(self, run: PlanRun, item: PlanRunItem) -> str:
        return item.plan_item_id or f"{run.plan_id}:{item.device_name}:{item.task_id or item.task_name}"

    def _is_bmc_item(self, item: PlanRunItem) -> bool:
        task_type = (item.task_type or "").upper()
        exec_mode = (item.execution_mode or "").upper()
        return task_type == "BMC" or exec_mode in ("BMC_URL", "BMC_ACTIONS")

    def _bmc_endpoint_key(self, item: PlanRunItem) -> str:
        if not self._is_bmc_item(item) or item._device is None or item._task is None:
            return ""
        try:
            from ..models.task_plan import TaskPlan
            return TaskPlan(device=item._device, task=item._task).endpoint_key
        except Exception:
            if item.lock_uri:
                return item.lock_uri
            bmc_ip = getattr(item._device, "bmc_ip", "") or ""
            return f"BMC:{bmc_ip}:443" if bmc_ip else ""

    def _collect_bmc_session_group(
        self, items: list[PlanRunItem], start_index: int,
    ) -> list[PlanRunItem]:
        first = items[start_index]
        endpoint_key = self._bmc_endpoint_key(first)
        if not endpoint_key:
            return [first]

        group: list[PlanRunItem] = []
        idx = start_index
        while idx < len(items):
            item = items[idx]
            if not self._is_bmc_item(item):
                break
            if self._bmc_endpoint_key(item) != endpoint_key:
                break
            group.append(item)
            idx += 1
        return group

    def _execute_real_bmc_group(
        self, run: PlanRun, items: list[PlanRunItem], runner: Any,
        cb: PlanItemStatusCallbackClient,
    ) -> None:
        for item in items:
            self._mark_item_started(run, item, cb)

        owner = f"{run.plan_id}:bmc-session:{self._bmc_endpoint_key(items[0])}"
        acquired_locks: list[str] = []
        for lock_uri in dict.fromkeys(item.lock_uri for item in items if item.lock_uri):
            if not self._lock_mgr.acquire(lock_uri, owner):
                for item in items:
                    item.status = "FAILED"
                    item.error_message = f"LOCK_CONFLICT: {lock_uri}"
                    item.add_info_event("ERROR", f"Lock conflict: {lock_uri}")
                for held in reversed(acquired_locks):
                    self._lock_mgr.release(held, owner)
                for item in items:
                    self._mark_item_finished(run, item, cb)
                return
            acquired_locks.append(lock_uri)

        try:
            payloads = [self._build_job_payload(item) for item in items]
            results = runner.run_bmc_session_group(payloads)
        except Exception as exc:
            for item in items:
                item.status = "FAILED"
                item.error_message = f"RUNNER_CRASH: {exc}"
                item.add_info_event("ERROR", item.error_message)
        else:
            for idx, item in enumerate(items):
                if idx >= len(results):
                    item.status = "FAILED"
                    item.error_message = "RUNNER_RESULT_MISSING"
                    item.add_info_event("ERROR", item.error_message)
                    continue
                self._apply_real_result(item, results[idx])
        finally:
            for lock_uri in reversed(acquired_locks):
                self._lock_mgr.release(lock_uri, owner)

        for item in items:
            self._mark_item_finished(run, item, cb)

    def _build_callback_body(self, run: PlanRun, item: PlanRunItem) -> dict[str, Any]:
        return self._callback_delivery.build_callback_body(run, item)

    def _deliver_item_status(self, run: PlanRun, item: PlanRunItem,
                             cb: PlanItemStatusCallbackClient) -> None:
        self._callback_delivery.deliver_item_status(run, item, cb)

    def _deliver_plan_summary(self, run: PlanRun, cb: PlanItemStatusCallbackClient) -> None:
        self._callback_delivery.deliver_plan_summary(run, cb)

    def _process_outbox_result(
        self, outbox, outbox_items: list,
        result: Any, callback_url: str, run_id: str = "",
    ):
        self._callback_delivery.process_outbox_result(
            outbox, outbox_items, result, callback_url, run_id,
        )

    def _execute_fake(self, item: PlanRunItem):
        time.sleep(0.001)
        item.status = "SUCCESS"
        item.error_message = None
        item.add_info_event(
            "INFO",
            f"Fake execution completed: planItemId={item.plan_item_id} "
            f"device={item.device_name} task={item.task_name}",
        )

    def _execute_real(self, item: PlanRunItem, runner: Any):
        task_type = item.task_type.upper()
        exec_mode = item.execution_mode.upper()

        if task_type not in ("BMC", "SSH") and exec_mode not in ("BMC_URL", "BMC_ACTIONS", "SSH_CMD"):
            item.status = "FAILED"
            item.error_message = f"UNSUPPORTED_TASK_TYPE: {task_type}/{exec_mode}"
            item.add_info_event("ERROR", item.error_message)
            return

        try:
            job_payload = self._build_job_payload(item)
        except Exception as e:
            item.status = "FAILED"
            item.error_message = f"PAYLOAD_BUILD_FAILED: {e}"
            item.add_info_event("ERROR", item.error_message)
            return

        try:
            result = runner.run_job(job_payload)
        except Exception as e:
            item.status = "FAILED"
            item.error_message = f"RUNNER_CRASH: {e}"
            item.add_info_event("ERROR", item.error_message)
            return

        self._apply_real_result(item, result)

    def _apply_real_result(self, item: PlanRunItem, result: Any) -> None:
        if result.status == "SUCCEEDED":
            item.status = "SUCCESS"
            item.error_message = None
            item._execution_result = getattr(result, "execution_result", None)
            item.add_info_event("INFO", f"Real execution succeeded")
        elif result.status == "TIMEOUT":
            item.status = "FAILED"
            item.error_message = f"TIMEOUT: {result.error.get('message', '') if result.error else 'timeout'}"
            item._execution_result = getattr(result, "execution_result", None)
            item.add_info_event("ERROR", item.error_message)
        else:
            item.status = "FAILED"
            item.error_message = result.error.get("message", "FAILED") if result.error else "FAILED"
            item._execution_result = getattr(result, "execution_result", None)
            item.add_info_event("ERROR", item.error_message)

    def _build_job_payload(self, item: PlanRunItem) -> dict[str, Any]:
        return self._job_payload_builder.build(item)

    def _write_plan_result_reports(self, run: PlanRun) -> None:
        """Write batch-level report files into outputRoot after all items finish."""
        self._result_reporter.write(run)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        """Get plan summary by plan_id. Returns None if not found."""
        run = self._get_plan(str(plan_id))
        if run is None:
            return None
        return self._public_plan(run, include_items=False)

    def get_plan_items(self, plan_id: int) -> dict[str, Any] | None:
        """Get plan items with per-item details. Returns None if plan not found."""
        run = self._get_plan(str(plan_id))
        if run is None:
            return None
        return self._public_plan(run, include_items=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get plan summary by run_id. Returns None if not found."""
        run = self._get_run_by_id(run_id)
        if run is None:
            return None
        return self._public_plan(run, include_items=False)

    def get_run_items(self, run_id: str) -> dict[str, Any] | None:
        """Get plan items by run_id. Returns None if not found."""
        run = self._get_run_by_id(run_id)
        if run is None:
            return None
        return self._public_plan(run, include_items=True)

    def retry_pending_callbacks(
        self,
        plan_id: int | str,
        callback_url: str = "",
        mode: str = "batch",
    ) -> dict[str, Any]:
        """Retry due callback outbox items for a plan.

        The outbox persists redacted callbackUrl values. For safety, retry uses
        the explicit callback_url argument first, then the current run/env/registry
        URL resolution path. No local run status changes on retry failure.
        """
        run = self._get_plan(str(plan_id))
        return self._callback_delivery.retry_pending_callbacks(
            plan_id=plan_id,
            run=run,
            callback_url=callback_url,
            mode=mode,
            transport_factory=self._resolve_transport,
        )

    def run_by_plan_id(self, plan_id: int):
        """Execute plan synchronously (for testing).
        If plan is already running (background thread), wait for it.

        Uses _resolve_transport() for transport selection, matching the
        production code path in start_plan_run().
        """
        run = self._get_plan(str(plan_id))
        if run is None:
            return
        if run.status == "COMPLETED":
            return
        with self._runs_lock:
            thread = self._run_threads.get(str(plan_id))
        if run.status == "RUNNING" and thread is not None and thread.is_alive():
            thread.join()
            return
        # P2-1: use _resolve_transport() instead of self._cb_transport or FakeCallbackTransport()
        transport = self._resolve_transport(run.item_status_url)
        cb = PlanItemStatusCallbackClient(transport=transport)
        self._execute_run(run, cb)
