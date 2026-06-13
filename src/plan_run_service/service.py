"""
PlanRunService — reads latest Excel, expands devices×tasks, executes with
FakeRunner or RealRunnerAdapter, sends 8-field plan-item status callbacks.
"""

from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..plan_item_status_callback_client import (
    PlanItemStatusCallbackClient,
    FakeCallbackTransport,
    HttpCallbackTransport,
    CallbackResult,
    build_callback_item,
    validate_callback_url,
)
from ..resource_lock_manager import ResourceLockManager
from ..utils.sensitive import redact_sensitive_text, redact_url_for_log

logger = logging.getLogger("bmc_auto_capture.plan_run")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunConfigSnapshot:
    """Immutable snapshot of latest Excel config at :run startup.

    Once bound to a PlanRun, the run uses ONLY this snapshot — never re-reads
    get_latest() or _get_latest_excel().  Updating latest.json after run start
    does not affect any already-running plan.

    Contains only excelHash + storedPath — no configId/configVersion exposed.
    """
    excel_hash: str
    stored_path: str
    original_filename: str = ""
    source: str = ""
    activated_at: str = ""
    device_count: int = 0
    enabled_device_count: int = 0
    task_count: int = 0
    enabled_task_count: int = 0

    @classmethod
    def from_latest_meta(cls, meta: dict[str, Any]) -> RunConfigSnapshot:
        return cls(
            excel_hash=meta.get("excelHash", ""),
            stored_path=meta.get("storedPath", ""),
            original_filename=meta.get("originalFilename", ""),
            source=meta.get("source", ""),
            activated_at=meta.get("activatedAt", ""),
            device_count=meta.get("deviceCount", 0),
            enabled_device_count=meta.get("enabledDeviceCount", 0),
            task_count=meta.get("taskCount", 0),
            enabled_task_count=meta.get("enabledTaskCount", 0),
        )


@dataclass
class PlanRunItem:
    plan_id: int | str
    device_name: str
    task_name: str
    device_group: str = ""
    task_type: str = ""
    execution_mode: str = ""
    lock_uri: str = ""
    status: str = "PENDING"
    error_message: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    info_events: list[dict[str, Any]] = field(default_factory=list)
    # Device/task raw refs for real runner conversion
    _device: Any = None
    _task: Any = None

    def add_info_event(self, level: str, message: str):
        self.info_events.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        })


@dataclass
class PlanRun:
    """One plan run, identified by plan_id (the single business plan ID).

    No serverPlanId, callbackPlanId, or executorPlanId.
    No configVersion (use config_snapshot for everything).
    """
    plan_id: int | str
    run_id: str = ""
    excel_hash: str = ""
    status: str = "ACCEPTED"
    runner_mode: str = "fake"
    items: list[PlanRunItem] = field(default_factory=list)
    updater: str = "downstream-system"
    item_status_url: str = ""
    callback_mode: str = "batch"  # "batch" or "single"
    started_at: float = 0.0
    finished_at: float = 0.0
    # ISSUE-001: immutable config snapshot bound at :run startup
    config_snapshot: RunConfigSnapshot | None = None

    @property
    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.items),
            "success": sum(1 for i in self.items if i.status == "SUCCESS"),
            "failed": sum(1 for i in self.items if i.status == "FAILED"),
            "in_progress": sum(1 for i in self.items if i.status == "IN_PROGRESS"),
            "pending": sum(1 for i in self.items if i.status == "PENDING"),
        }

    @property
    def is_external(self) -> bool:
        return bool(self.excel_hash)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_excel_store: dict[str, Any] = {}
_store_lock = threading.Lock()


def _safe_state_id(value: str) -> str:
    """Return a path-safe id for run/plan state files."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("state id is empty")
    if ".." in raw or raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", raw):
        raise ValueError("state id contains path traversal")
    if any(sep and sep in raw for sep in (os.sep, os.altsep)):
        raise ValueError("state id contains path separator")
    return raw


def _fmt_ts(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


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
                 allow_real_runner: bool = False):
        if workspace_root is None:
            from ..excel_config_store import _resolve_workspace
            workspace_root = str(_resolve_workspace())
        self._runs: dict[str, PlanRun] = {}
        self._runs_by_run_id: dict[str, PlanRun] = {}
        self._runs_lock = threading.Lock()
        self._cb_transport = callback_transport
        self._lock_mgr = lock_manager or ResourceLockManager()
        self._workspace_root = workspace_root
        self._allow_real_runner = allow_real_runner or _env_truthy("EXECUTOR_ENABLE_REAL_RUNNER")
        self._state_root = Path(self._workspace_root) / "executor_state"
        self._runs_state_dir = self._state_root / "runs"
        self._plans_state_dir = self._state_root / "plans"
        self._load_persisted_runs()

        # P1-4: use_http_callback is deprecated — transport selection now uses
        # _resolve_transport() which auto-detects HttpCallbackTransport when
        # itemStatusUrl is provided.  The parameter is kept for backward
        # compatibility only.
        if use_http_callback:
            logger.warning(
                "PlanRunService(use_http_callback=True) is deprecated. "
                "Transport is now auto-selected via _resolve_transport() based on "
                "itemStatusUrl. Pass callback_transport=HttpCallbackTransport() "
                "if you need an explicit HTTP transport."
            )
        self._use_http = use_http_callback  # deprecated, kept for compat

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
        ok, reason = validate_callback_url(url)
        if ok:
            return {"ok": True}
        return {
            "ok": False,
            "reason": reason,
            "message": "callback.itemStatusUrl is not allowed by executor callback URL policy",
        }

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

        enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
        enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]

        items: list[PlanRunItem] = []
        for device in enabled_devices:
            for task in enabled_tasks:
                match_group = getattr(task, "match_group", "") or ""
                dg = getattr(device, "device_group", "") or ""
                if match_group:
                    allowed_groups = [g.strip().upper() for g in match_group.split("/") if g.strip()]
                    if dg.upper() not in allowed_groups:
                        continue
                lock_uri = _derive_lock_uri(device, task)
                items.append(PlanRunItem(
                    plan_id=plan_id, device_name=getattr(device, "device_name", ""),
                    task_name=getattr(task, "task_name", ""), device_group=dg,
                    task_type=getattr(task, "task_type", ""),
                    execution_mode=getattr(task, "execution_mode", ""),
                    lock_uri=lock_uri, _device=device, _task=task,
                ))

        run_id = f"plan-{plan_id}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        run = PlanRun(
            plan_id=plan_id, run_id=run_id, excel_hash=snapshot.excel_hash,
            status="RUNNING", runner_mode=runner_mode,
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
                "callback configured: url=%s mode=%s transport=%s planId=%s runId=%s itemCount=%d",
                redact_url_for_log(item_status_url), callback_mode, transport_mode,
                plan_id, run_id, len(items),
            )
        else:
            logger.info("callback not configured: no itemStatusUrl provided")

        t = threading.Thread(target=self._execute_run, args=(run, cb), daemon=True)
        t.start()

        return {
            "accepted": True, "excelHash": excel_hash,
            "planId": plan_id, "runId": run_id, "status": "ACCEPTED",
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
        return {
            "deviceName": item.device_name,
            "taskName": item.task_name,
            "status": item.status,
            "errorMessage": redact_sensitive_text(item.error_message or "") if item.error_message else None,
            "startedAt": _fmt_ts(item.started_at) if item.started_at else None,
            "finishedAt": _fmt_ts(item.finished_at) if item.finished_at else None,
            "infoEvents": item.info_events,
        }

    def _public_plan(self, run: PlanRun, include_items: bool = False) -> dict[str, Any]:
        result = {
            "planId": run.plan_id,
            "runId": run.run_id,
            "status": run.status,
            "summary": run.summary,
            "excelHash": run.excel_hash,
            "startedAt": _fmt_ts(run.started_at),
            "finishedAt": _fmt_ts(run.finished_at),
            "errorMessage": None,
            "infoEvents": [
                {
                    "timestamp": _fmt_ts(run.started_at),
                    "level": "INFO",
                    "message": f"Plan started: planId={run.plan_id}",
                } if run.started_at else None,
                {
                    "timestamp": _fmt_ts(run.finished_at),
                    "level": "INFO",
                    "message": f"Plan finished: status={run.status}",
                } if run.finished_at else None,
            ],
        }
        result["infoEvents"] = [ev for ev in result["infoEvents"] if ev]
        if include_items:
            result["items"] = [self._public_item(item) for item in run.items]
        return result

    def _run_to_state(self, run: PlanRun) -> dict[str, Any]:
        return {
            "version": 1,
            "planId": str(run.plan_id),
            "runId": run.run_id,
            "excelHash": run.excel_hash,
            "status": run.status,
            "runnerMode": run.runner_mode,
            "updater": run.updater,
            "callbackMode": run.callback_mode,
            "startedAt": run.started_at,
            "finishedAt": run.finished_at,
            "items": [
                {
                    "planId": str(item.plan_id),
                    "deviceName": item.device_name,
                    "taskName": item.task_name,
                    "deviceGroup": item.device_group,
                    "taskType": item.task_type,
                    "executionMode": item.execution_mode,
                    "status": item.status,
                    "errorMessage": redact_sensitive_text(item.error_message or "") if item.error_message else None,
                    "startedAt": item.started_at,
                    "finishedAt": item.finished_at,
                    "infoEvents": item.info_events,
                }
                for item in run.items
            ],
        }

    def _state_to_run(self, data: dict[str, Any]) -> PlanRun | None:
        plan_id = data.get("planId", "")
        run_id = data.get("runId", "")
        if not plan_id or not run_id:
            return None
        status = str(data.get("status", ""))
        if status in ("ACCEPTED", "RUNNING"):
            status = "INTERRUPTED"
        items: list[PlanRunItem] = []
        for raw in data.get("items", []) or []:
            if not isinstance(raw, dict):
                continue
            item_status = str(raw.get("status", "PENDING"))
            if item_status in ("PENDING", "IN_PROGRESS", "RUNNING"):
                item_status = "FAILED" if status == "INTERRUPTED" else item_status
            items.append(PlanRunItem(
                plan_id=plan_id,
                device_name=str(raw.get("deviceName", "")),
                task_name=str(raw.get("taskName", "")),
                device_group=str(raw.get("deviceGroup", "")),
                task_type=str(raw.get("taskType", "")),
                execution_mode=str(raw.get("executionMode", "")),
                status=item_status,
                error_message=raw.get("errorMessage"),
                started_at=float(raw.get("startedAt", 0.0) or 0.0),
                finished_at=float(raw.get("finishedAt", 0.0) or 0.0),
                info_events=list(raw.get("infoEvents", []) or []),
            ))
        return PlanRun(
            plan_id=plan_id,
            run_id=str(run_id),
            excel_hash=str(data.get("excelHash", "")),
            status=status or "UNKNOWN",
            runner_mode=str(data.get("runnerMode", "fake")),
            items=items,
            updater=str(data.get("updater", "downstream-system")),
            item_status_url="",
            callback_mode=str(data.get("callbackMode", "batch")),
            started_at=float(data.get("startedAt", 0.0) or 0.0),
            finished_at=float(data.get("finishedAt", 0.0) or 0.0),
        )

    def _persist_run(self, run: PlanRun) -> None:
        if not run.run_id:
            return
        try:
            safe_run_id = _safe_state_id(run.run_id)
            safe_plan_id = _safe_state_id(str(run.plan_id))
            self._runs_state_dir.mkdir(parents=True, exist_ok=True)
            (self._plans_state_dir / safe_plan_id).mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._run_to_state(run), ensure_ascii=False, indent=2)
            for target in (
                self._runs_state_dir / f"{safe_run_id}.json",
                self._plans_state_dir / safe_plan_id / "latest_run.json",
            ):
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, target)
        except Exception as exc:
            logger.warning("PlanRun state persist failed: %s", redact_sensitive_text(str(exc)))

    def _load_persisted_runs(self) -> None:
        if not self._runs_state_dir.exists():
            return
        loaded = 0
        for path in self._runs_state_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                run = self._state_to_run(data)
                if run is None:
                    continue
                self._runs_by_run_id[run.run_id] = run
                current = self._runs.get(str(run.plan_id))
                if current is None or run.started_at >= current.started_at:
                    self._runs[str(run.plan_id)] = run
                loaded += 1
            except Exception as exc:
                logger.warning("PlanRun state load skipped for %s: %s", path.name, redact_sensitive_text(str(exc)))
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

        # 2. Fallback to in-memory _get_latest_excel (legacy path)
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

        # Legacy in-memory only path
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
    # Start plan run (legacy)
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

        enabled_devices = [d for d in devices if getattr(d, "enabled", True)]
        enabled_tasks = [t for t in tasks if getattr(t, "enabled", True)]

        items: list[PlanRunItem] = []
        for device in enabled_devices:
            for task in enabled_tasks:
                match_group = getattr(task, "match_group", "") or ""
                dg = getattr(device, "device_group", "") or ""
                if match_group:
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

        run_id = f"plan-{plan_id}-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        run = PlanRun(
            plan_id=plan_id, run_id=run_id, status="RUNNING",
            runner_mode=runner_mode,
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
                "callback configured: url=%s mode=%s transport=%s planId=%s runId=%s itemCount=%d",
                redact_url_for_log(item_status_url), callback_mode, transport_mode,
                plan_id, run_id, len(items),
            )
        else:
            logger.info("callback not configured: no itemStatusUrl provided")

        t = threading.Thread(target=self._execute_run, args=(run, cb), daemon=True)
        t.start()

        return {
            "accepted": True, "planId": plan_id,
            "runId": run_id,
            "status": "ACCEPTED",
            "excelHash": snapshot.excel_hash,
            "message": "plan run accepted",
        }

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def _resolve_callback_url(self, run: PlanRun) -> str:
        """Resolve callback URL with priority:
        1. master_registry active server
        2. request callback.itemStatusUrl
        3. env var EXECUTOR_PLAN_ITEM_STATUS_URL
        4. empty string (no callback)
        """
        # Priority 1: master_registry
        registry_url = os.environ.get("EXECUTOR_MASTER_REGISTRY_URL", "")
        if registry_url:
            try:
                from ..server_registry_client import discover_callback_url
                discovered = discover_callback_url()
                if discovered:
                    check = self._validate_callback_url(discovered)
                    if not check.get("ok"):
                        logger.warning("Callback URL from registry rejected: %s", check.get("reason", "INVALID_CALLBACK_URL"))
                        return ""
                    logger.info(
                        "Callback URL resolved via registry: %s",
                        redact_url_for_log(discovered),
                    )
                    return discovered
            except Exception as e:
                logger.warning("CALLBACK_REGISTRY_RESOLVE_FAILED: %s", e)

        # Priority 2: request callback.itemStatusUrl
        if run.item_status_url:
            check = self._validate_callback_url(run.item_status_url)
            if not check.get("ok"):
                logger.warning("Callback URL from request rejected: %s", check.get("reason", "INVALID_CALLBACK_URL"))
                return ""
            logger.info(
                "Callback URL from request: %s",
                redact_url_for_log(run.item_status_url),
            )
            return run.item_status_url

        # Priority 3: environment variable
        env_url = os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", "")
        if env_url:
            check = self._validate_callback_url(env_url)
            if not check.get("ok"):
                logger.warning("Callback URL from env rejected: %s", check.get("reason", "INVALID_CALLBACK_URL"))
                return ""
            logger.info("Callback URL from env: %s", redact_url_for_log(env_url))
            return env_url

        # Priority 4: not configured
        return ""

    def _execute_run(self, run: PlanRun, cb: PlanItemStatusCallbackClient):
        is_real = run.runner_mode == "real"
        runner = None
        if is_real:
            from ..job_runner_adapter import RealRunnerAdapter
            runner = RealRunnerAdapter()

        # Execute each item (serial, same as before)
        for item in run.items:
            item.status = "IN_PROGRESS"
            item.started_at = time.time()
            item.add_info_event("INFO", f"PlanItem started: device={item.device_name} task={item.task_name}")
            self._persist_run(run)

            # Lock
            if item.lock_uri:
                if not self._lock_mgr.acquire(item.lock_uri, f"{run.plan_id}:{item.device_name}:{item.task_name}"):
                    item.status = "FAILED"
                    item.error_message = f"LOCK_CONFLICT: {item.lock_uri}"
                    item.finished_at = time.time()
                    item.add_info_event("ERROR", f"Lock conflict: {item.lock_uri}")
                    self._persist_run(run)
                    continue

            try:
                if is_real:
                    self._execute_real(item, runner)
                else:
                    self._execute_fake(item)
            finally:
                if item.lock_uri:
                    self._lock_mgr.release(item.lock_uri, f"{run.plan_id}:{item.device_name}:{item.task_name}")

            item.finished_at = time.time()
            if item.status == "SUCCESS":
                item.add_info_event("INFO", f"PlanItem completed: status={item.status}")
            else:
                item.add_info_event("ERROR", f"PlanItem failed: status={item.status} error={item.error_message}")
            self._persist_run(run)

        run.finished_at = time.time()
        self._persist_run(run)

        # --- ISSUE-005: CallbackOutbox ---
        # Write outbox + attempt delivery BEFORE marking run COMPLETED.
        # Delivery failures NEVER change local plan/run status.
        self._deliver_via_outbox(run, cb)

        run.status = "COMPLETED"
        self._persist_run(run)

    def _deliver_via_outbox(self, run: PlanRun, cb: PlanItemStatusCallbackClient):
        """ISSUE-005: Write callback items to outbox, then attempt delivery.

        Local plan/run status is never affected by callback outcome.
        Callback body planId comes from run.plan_id (single business plan ID).
        Batch payload includes runId, summary, and per-item startedAt/finishedAt.
        """
        from ..callback_outbox import (
            CallbackOutbox, CallbackOutboxItem,
            build_outbox_item_from_callback_body,
            classify_callback_error,
        )

        # --- Resolve callback URL ---
        callback_url = self._resolve_callback_url(run)
        plan_id = str(run.plan_id)
        url_configured = bool(callback_url and plan_id)

        if not callback_url:
            logger.info("CALLBACK_URL_NOT_CONFIGURED: no callback URL resolved")
        if not plan_id:
            logger.warning("CALLBACK_PLAN_ID_MISSING: plan_id is empty")

        # --- Build callback items with startedAt/finishedAt ---
        outbox = CallbackOutbox(plan_id, workspace_root=self._workspace_root)
        outbox_items: list[CallbackOutboxItem] = []
        cb_items: list[dict[str, Any]] = []

        for item in run.items:
            started_at_iso = (
                datetime.fromtimestamp(item.started_at, tz=timezone.utc).isoformat()
                if item.started_at else None
            )
            finished_at_iso = (
                datetime.fromtimestamp(item.finished_at, tz=timezone.utc).isoformat()
                if item.finished_at else None
            )
            cb_body = build_callback_item(
                plan_id=plan_id,
                device_name=item.device_name,
                task_name=item.task_name,
                status=item.status,
                updater=run.updater,
                error_message=item.error_message,
                started_at=started_at_iso,
                finished_at=finished_at_iso,
            )
            cb_items.append(cb_body)

            oi = build_outbox_item_from_callback_body(
                plan_id=cb_body["planId"],
                device_name=cb_body["deviceName"],
                task_name=cb_body["taskName"],
                status=cb_body["status"],
                updater=cb_body["updater"],
                error_message=cb_body["errorMessage"],
                callback_url=callback_url if url_configured else "",
            )
            if not url_configured:
                oi.delivery_status = "URL_NOT_CONFIGURED"
            outbox_items.append(oi)

        if not outbox_items:
            logger.info("No callback items to send (plan has zero items)")
            return

        outbox.append_batch(outbox_items)
        transport_mode = "http" if isinstance(cb.transport, HttpCallbackTransport) else "fake"
        logger.info(
            "CallbackOutbox: %d items written for planId=%s (url_configured=%s, transport=%s)",
            len(outbox_items), plan_id, url_configured, transport_mode,
        )

        if not url_configured:
            return

        # --- Build summary ---
        summary = {
            "total": len(run.items),
            "success": sum(1 for it in run.items if it.status == "SUCCESS"),
            "failed": sum(1 for it in run.items if it.status == "FAILED"),
            "in_progress": sum(1 for it in run.items if it.status in ("IN_PROGRESS", "RUNNING")),
            "pending": sum(1 for it in run.items if it.status == "PENDING"),
        }

        # --- Attempt delivery ---
        logger.info(
            "callback send start: runId=%s planId=%s itemCount=%d mode=%s url=%s",
            run.run_id, plan_id, len(cb_items), run.callback_mode,
            redact_url_for_log(callback_url),
        )

        try:
            if run.callback_mode == "batch":
                result: CallbackResult = cb.send_batch(
                    callback_url, cb_items,
                    run_id=run.run_id, summary=summary,
                )
                self._process_outbox_result(outbox, outbox_items, result, callback_url, run.run_id)
            else:
                # P1-2: single mode sends the same 8-field items as batch mode
                # (planId, deviceName, taskName, status, updater, errorMessage,
                #  startedAt, finishedAt).  Do NOT use oi.to_callback_body()
                # which only returns 6 fields — that is the internal outbox
                # format, not the external callback payload.
                for idx, oi in enumerate(outbox_items):
                    r = cb.send_single(callback_url, cb_items[idx])
                    single_result = CallbackResult(
                        total=1, success=1 if r.ok else 0,
                        failed=0 if r.ok else 1, batches=1,
                        last_error=r.last_error,
                    )
                    self._process_outbox_result(outbox, [oi], single_result, callback_url, run.run_id)
        except Exception as e:
            logger.error(
                "callback send failed: runId=%s exception=%s",
                run.run_id, redact_sensitive_text(str(e)[:200]),
            )

    def _process_outbox_result(
        self, outbox, outbox_items: list,
        result: Any, callback_url: str, run_id: str = "",
    ):
        """ISSUE-005: Update outbox items based on delivery result."""
        from ..callback_outbox import classify_callback_error

        if result.ok:
            for oi in outbox_items:
                outbox.mark_sent(oi.outbox_id)
            logger.info(
                "callback send success: runId=%s statusCode=200 itemCount=%d url=%s",
                run_id, len(outbox_items), redact_url_for_log(callback_url),
            )
            return

        # Partial or full failure — mark each item
        error_msg = getattr(result, 'last_error', None) or "CALLBACK_FAILED"
        retryable, _ = classify_callback_error(error_msg)

        for oi in outbox_items:
            outbox.mark_failed(
                oi.outbox_id, error_message=error_msg,
                retryable=retryable,
            )

        status = "FAILED_RETRYABLE" if retryable else "FAILED_FINAL"
        from ..utils.sensitive import redact_sensitive_text
        logger.warning(
            "callback send failed: runId=%s itemCount=%d status=%s url=%s error=%s",
            run_id, len(outbox_items), status, redact_url_for_log(callback_url),
            redact_sensitive_text((error_msg or "")[:120]),
        )

    def _execute_fake(self, item: PlanRunItem):
        time.sleep(0.001)
        item.status = "SUCCESS"
        item.error_message = None
        item.add_info_event("INFO", f"Fake execution completed: device={item.device_name} task={item.task_name}")

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

        if result.status == "SUCCEEDED":
            item.status = "SUCCESS"
            item.error_message = None
            item.add_info_event("INFO", f"Real execution succeeded")
        elif result.status == "TIMEOUT":
            item.status = "FAILED"
            item.error_message = f"TIMEOUT: {result.error.get('message', '') if result.error else 'timeout'}"
            item.add_info_event("ERROR", item.error_message)
        else:
            item.status = "FAILED"
            item.error_message = result.error.get("message", "FAILED") if result.error else "FAILED"
            item.add_info_event("ERROR", item.error_message)

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
            "retry_count": int(getattr(task, "retry_count", 0) or 0),
            "output_dir_template": getattr(task, "output_dir_template", "{device_name}/{task_name}") or "{device_name}/{task_name}",
            "image_name_template": getattr(task, "image_name_template", "{device_name}_{task_name}_{step}_{timestamp}") or "{device_name}_{task_name}_{step}_{timestamp}",
            "full_screenshot": bool(getattr(task, "full_screenshot", False)),
            "screenshot_mode": getattr(task, "screenshot_mode", "auto") or "auto",
        }

        return {
            "device_snapshot": device_snapshot,
            "task_snapshot": task_snapshot,
        }

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
        if mode not in ("batch", "single"):
            return {"accepted": False, "planId": plan_id, "status": "FAILED",
                    "message": f"INVALID_CALLBACK_MODE: {mode}"}

        run = self._get_plan(str(plan_id))
        resolved_url = callback_url
        if resolved_url:
            check = self._validate_callback_url(resolved_url)
            if not check.get("ok"):
                return {"accepted": False, "planId": plan_id,
                        "status": "FAILED",
                        "message": check.get("message", "Invalid callback URL")}
        elif run is not None:
            resolved_url = self._resolve_callback_url(run)
        elif os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", ""):
            resolved_url = os.environ.get("EXECUTOR_PLAN_ITEM_STATUS_URL", "")
            check = self._validate_callback_url(resolved_url)
            if not check.get("ok"):
                resolved_url = ""

        if not resolved_url:
            return {"accepted": False, "planId": plan_id, "status": "FAILED",
                    "message": "No valid callback URL available for retry"}

        from ..callback_outbox import CallbackOutbox
        outbox = CallbackOutbox(str(plan_id), workspace_root=self._workspace_root)
        pending = outbox.get_pending()
        if not pending:
            return {
                "accepted": True, "planId": plan_id,
                "runId": run.run_id if run else "",
                "attempted": 0, "sent": 0, "failed": 0,
                "pendingAfter": 0, "status": "NO_PENDING",
                "message": "no pending callbacks",
            }

        transport = self._resolve_transport(resolved_url)
        cb = PlanItemStatusCallbackClient(transport=transport)
        bodies = [it.to_callback_body() for it in pending]
        attempted = len(pending)
        sent = 0
        failed = 0
        summary = run.summary if run else None

        if mode == "batch":
            result = cb.send_batch(
                resolved_url, bodies,
                run_id=run.run_id if run else "",
                summary=summary,
            )
            if result.ok:
                for item in pending:
                    outbox.mark_sent(item.outbox_id)
                sent = attempted
            else:
                self._process_outbox_result(outbox, pending, result, resolved_url, run.run_id if run else "")
                failed = attempted
        else:
            for item in pending:
                result = cb.send_single(resolved_url, item.to_callback_body())
                if result.ok:
                    outbox.mark_sent(item.outbox_id)
                    sent += 1
                else:
                    self._process_outbox_result(outbox, [item], result, resolved_url, run.run_id if run else "")
                    failed += 1

        pending_after = len(outbox.get_pending())
        return {
            "accepted": True,
            "planId": plan_id,
            "runId": run.run_id if run else "",
            "attempted": attempted,
            "sent": sent,
            "failed": failed,
            "pendingAfter": pending_after,
            "status": "RETRIED",
            "message": "pending callbacks retried",
        }

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
        # P2-1: use _resolve_transport() instead of self._cb_transport or FakeCallbackTransport()
        transport = self._resolve_transport(run.item_status_url)
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
