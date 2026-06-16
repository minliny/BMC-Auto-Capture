"""
RunDispatchService — plan import, run dispatch, execute with locking, callback.

DEPRECATED/ISOLATED: This service uses its own internal run_id and is NOT part
of the unified planId model.  It does NOT contaminate PlanRunService,
CallbackOutbox, or executor_state/plans/{planId}.  New code should use
PlanRunService (/executor/v1/plans/{plan_id}:run) instead.
"""

from __future__ import annotations
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..plan_catalog.models import PlannedTask
from ..plan_catalog.planner import PlanCatalogPlanner
from ..plan_catalog.store import TaskCatalogStore
from ..resource_lock_manager import ResourceLockManager
from ..job_runner_adapter import FakeRunner, RealRunnerAdapter, JobResult
from ..server_callback_client import (
    ServerCallbackClient,
    FakeCallbackTransport,
    HttpCallbackTransport,
)
from ..utils.sensitive import redact_sensitive_text

logger = logging.getLogger("bmc_auto_capture.run_dispatch")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Status enums
# ---------------------------------------------------------------------------

class RunStatus:
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    CANCELED = "CANCELED"


class TaskRunStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"
    CALLBACK_FAILED = "CALLBACK_FAILED"
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Runtime task record
# ---------------------------------------------------------------------------

@dataclass
class _TaskRun:
    task_id: str
    run_id: str = ""
    status: str = TaskRunStatus.QUEUED
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    last_callback_error: str = ""


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------

@dataclass
class _RunRecord:
    run_id: str
    plan_id: str = ""
    plan_hash: str = ""
    command_id: str = ""
    status: str = RunStatus.ACCEPTED
    callback_run_status_url: str = ""
    callback_task_status_url: str = ""
    callback_auth_token: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    tasks: dict[str, _TaskRun] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class RunDispatchService:
    """DEPRECATED: Orchestrates plan import → run dispatch → execute → callback.

    Isolated from the unified planId model. Uses internal run_id.
    """

    def __init__(
        self,
        executor_id: str = "exec-win-001",
        lock_manager: ResourceLockManager | None = None,
        runner_mode: str = "fake",
        runner: Any = None,
        callback_transport: Any = None,
        use_http_callback: bool = False,
        callback_timeout_seconds: float = 30.0,
        output_root: str = "./output_api_direct",
        allow_real_runner: bool = False,
    ):
        self.executor_id = executor_id
        self._lock_mgr = lock_manager or ResourceLockManager()

        # Runner
        if runner is not None:
            self._runner = runner
        elif runner_mode == "real":
            if not (allow_real_runner or _env_truthy("EXECUTOR_ENABLE_REAL_RUNNER")):
                raise ValueError(
                    "runner_mode=real requires RunDispatchService(allow_real_runner=True) "
                    "or EXECUTOR_ENABLE_REAL_RUNNER=1"
                )
            self._runner = RealRunnerAdapter(output_root=output_root)
        else:
            self._runner = FakeRunner()

        # Transport
        if callback_transport is not None:
            self._transport = callback_transport
        elif use_http_callback:
            self._transport = HttpCallbackTransport(timeout_seconds=callback_timeout_seconds)
        else:
            self._transport = FakeCallbackTransport()

        self._callback = ServerCallbackClient(executor_id=executor_id, transport=self._transport)

        # State
        self._plans: dict[str, dict] = {}  # plan_id -> {manifest, catalog, report}
        self._runs: dict[str, _RunRecord] = {}
        self._command_ids: set[str] = set()  # for idempotency
        self._state_lock = threading.Lock()
        self._pending: list[tuple[str, str]] = []  # (run_id, task_id)
        self._queue_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Plan import
    # ------------------------------------------------------------------

    def import_plan(self, excel_path: str, validation_json_path: str) -> dict[str, Any]:
        """Import Excel + validation.json, build plan. Returns summary."""
        planner = PlanCatalogPlanner(excel_path, validation_json_path)
        manifest, catalog, report = planner.build()

        if not report.is_valid:
            return {
                "accepted": False,
                "reason": "validation_failed",
                "validation_errors": report.error_count,
                "errors": [e.to_dict() for e in report.errors],
            }

        plan_id = manifest.plan_id
        with self._state_lock:
            self._plans[plan_id] = {
                "manifest": manifest,
                "catalog": catalog,
                "report": report,
            }

        return {
            "accepted": True,
            "plan_id": plan_id,
            "plan_hash": manifest.plan_hash,
            "task_count": manifest.task_count,
            "validation_errors": report.error_count,
        }

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._state_lock:
            entry = self._plans.get(plan_id)
        if entry is None:
            return None
        m = entry["manifest"]
        return {
            "plan_id": m.plan_id,
            "plan_hash": m.plan_hash,
            "planner_version": m.planner_version,
            "excel_sha256": m.excel_sha256,
            "validation_json_sha256": m.validation_json_sha256,
            "generated_at": m.generated_at,
            "task_count": m.task_count,
            "tasks": [t.to_dict() for t in m.tasks],
        }

    def get_plan_tasks(self, plan_id: str) -> list[dict[str, Any]] | None:
        with self._state_lock:
            entry = self._plans.get(plan_id)
        if entry is None:
            return None
        return [t.to_dict() for t in entry["manifest"].tasks]

    def get_plan_task(self, plan_id: str, task_id: str) -> dict[str, Any] | None:
        with self._state_lock:
            entry = self._plans.get(plan_id)
        if entry is None:
            return None
        t = entry["catalog"].get(task_id)
        if t is None:
            return None
        return t.to_catalog_dict()

    # ------------------------------------------------------------------
    # Run dispatch
    # ------------------------------------------------------------------

    def start_run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Accept a run dispatch. Returns accept/reject response."""
        command_id = request.get("command_id", "")
        run_id = request.get("run_id", "")
        plan_id = request.get("plan_id", "")
        plan_hash = request.get("plan_hash", "")
        callback = request.get("callback", {})

        # Idempotency
        with self._state_lock:
            if command_id and command_id in self._command_ids:
                existing = self._runs.get(run_id)
                return {
                    "accepted": False, "duplicate": True,
                    "run_id": run_id, "status": existing.status if existing else "?",
                }

        # Validate plan
        entry = self._plans.get(plan_id)
        if entry is None:
            return {"accepted": False, "reason": "plan_not_found", "plan_id": plan_id}

        manifest = entry["manifest"]
        if plan_hash and manifest.plan_hash != plan_hash:
            return {
                "accepted": False, "reason": "plan_hash_mismatch",
                "expected": manifest.plan_hash, "received": plan_hash,
            }

        catalog = entry["catalog"]

        # Build run
        run = _RunRecord(
            run_id=run_id, plan_id=plan_id, plan_hash=manifest.plan_hash,
            command_id=command_id, status=RunStatus.ACCEPTED,
            callback_run_status_url=callback.get("run_status_url", ""),
            callback_task_status_url=callback.get("task_status_url", ""),
            callback_auth_token=callback.get("auth_token", ""),
        )

        # Enqueue all tasks from catalog
        for pt in [catalog.get(tid) for tid in sorted(catalog.to_dict().keys())]:
            if pt is None or not pt.enabled:
                continue
            tr = _TaskRun(task_id=pt.task_id, run_id=run_id, status=TaskRunStatus.QUEUED)
            run.tasks[pt.task_id] = tr

        with self._state_lock:
            self._command_ids.add(command_id)
            self._runs[run_id] = run

        with self._queue_lock:
            for tid in run.tasks:
                self._pending.append((run_id, tid))

        return {
            "accepted": True, "run_id": run_id, "plan_id": plan_id,
            "plan_hash": manifest.plan_hash, "task_count": len(run.tasks),
            "status": RunStatus.ACCEPTED, "duplicate": False,
        }

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def run_pending_once(self) -> bool:
        """Execute one pending task. Returns True if processed."""
        with self._queue_lock:
            if not self._pending:
                return False
            run_id, task_id = self._pending.pop(0)

        self._execute_task(run_id, task_id)
        return True

    def run_all_pending(self) -> int:
        count = 0
        max_cycles = 5000
        for _ in range(max_cycles):
            if not self.run_pending_once():
                break
            count += 1
        return count

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        tasks = list(run.tasks.values())
        return {
            "run_id": run.run_id, "plan_id": run.plan_id, "status": run.status,
            "total_tasks": len(tasks),
            "queued": sum(1 for t in tasks if t.status == TaskRunStatus.QUEUED),
            "running": sum(1 for t in tasks if t.status == TaskRunStatus.RUNNING),
            "succeeded": sum(1 for t in tasks if t.status == TaskRunStatus.SUCCEEDED),
            "failed": sum(1 for t in tasks if t.status == TaskRunStatus.FAILED),
            "canceled": sum(1 for t in tasks if t.status == TaskRunStatus.CANCELED),
            "duration_ms": int((run.finished_at - run.started_at) * 1000) if run.finished_at > 0 else 0,
        }

    def get_run_tasks(self, run_id: str) -> list[dict[str, Any]] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return [self._task_run_to_dict(t) for t in run.tasks.values()]

    def get_run_task(self, run_id: str, task_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        tr = run.tasks.get(task_id)
        if tr is None:
            return None
        return self._task_run_to_dict(tr)

    def _task_run_to_dict(self, tr: _TaskRun) -> dict[str, Any]:
        entry = self._plans.get(self._runs.get(tr.run_id, _RunRecord("")).plan_id)
        catalog = entry["catalog"] if entry else None
        pt = catalog.get(tr.task_id) if catalog else None
        return {
            "run_id": tr.run_id, "task_id": tr.task_id,
            "status": tr.status,
            "task_no": pt.task_no if pt else "",
            "task_name": pt.task_name if pt else "",
            "task_type": pt.task_type if pt else "",
            "device_group": pt.device_group if pt else "",
            "lock_uri": pt.lock_uri if pt else "",
            "started_at": _fmt_ts(tr.started_at),
            "finished_at": _fmt_ts(tr.finished_at),
            "duration_ms": tr.duration_ms,
            "result": tr.result,
            "error": tr.error,
            "artifacts": tr.artifacts,
        }

    # ------------------------------------------------------------------
    # Internal: execute one task
    # ------------------------------------------------------------------

    def _execute_task(self, run_id: str, task_id: str):
        run = self._runs.get(run_id)
        if run is None:
            return
        tr = run.tasks.get(task_id)
        if tr is None:
            return

        entry = self._plans.get(run.plan_id)
        if entry is None:
            return
        catalog = entry["catalog"]
        pt = catalog.get(task_id)
        if pt is None:
            tr.status = TaskRunStatus.SKIPPED
            return

        lock_uri = pt.lock_uri

        acquired_lock = False

        # Acquire lock
        if lock_uri and not self._lock_mgr.acquire(lock_uri, task_id):
            with self._queue_lock:
                self._pending.append((run_id, task_id))
            return
        acquired_lock = bool(lock_uri)

        try:
            # Mark run RUNNING
            if run.status == RunStatus.ACCEPTED:
                run.status = RunStatus.RUNNING
                run.started_at = time.time()

            # Mark task RUNNING
            tr.status = TaskRunStatus.RUNNING
            tr.started_at = time.time()

            # Callback RUNNING
            try:
                self._send_task_callback(run, tr, "RUNNING")
            except Exception as e:
                logger.error(
                    "RunDispatch start callback crashed for run=%s task=%s: %s",
                    run_id,
                    task_id,
                    redact_sensitive_text(str(e)),
                )

            # Build job payload
            job_payload = {
                "job_id": f"{run_id}-{task_id}",
                "device_snapshot": pt.device_snapshot,
                "task_snapshot": pt.task_snapshot,
            }

            # Execute
            try:
                result = self._runner.run_job(job_payload)
            except Exception as e:
                result = JobResult(
                    status="FAILED", duration_ms=0,
                    error={
                        "code": "RUNNER_CRASH",
                        "message": redact_sensitive_text(str(e))[:200],
                        "retryable": False,
                        "category": "SYSTEM",
                    },
                )

            # Record result
            tr.status = result.status
            tr.finished_at = time.time()
            tr.duration_ms = result.duration_ms or int((tr.finished_at - tr.started_at) * 1000)
            tr.result = {"summary": f"EXEC_{result.status}"}
            tr.error = result.error
            tr.artifacts = result.artifacts

            # Callback finished
            cb_ok = True
            callback_error = ""
            if run.callback_task_status_url:
                try:
                    cb_ok = self._send_task_callback(run, tr, result.status)
                except Exception as e:
                    cb_ok = False
                    callback_error = redact_sensitive_text(str(e))
                    logger.error(
                        "RunDispatch finish callback crashed for run=%s task=%s: %s",
                        run_id,
                        task_id,
                        callback_error,
                    )

            if not cb_ok and run.callback_task_status_url:
                tr.status = TaskRunStatus.CALLBACK_FAILED
                tr.last_callback_error = callback_error or "Callback non-2xx or network error"
        except Exception as e:
            message = redact_sensitive_text(str(e))
            logger.error("RunDispatch task flow crashed for run=%s task=%s: %s", run_id, task_id, message)
            tr.status = TaskRunStatus.FAILED
            tr.finished_at = time.time()
            tr.duration_ms = int((tr.finished_at - (tr.started_at or tr.finished_at)) * 1000)
            tr.result = {"summary": "EXEC_FAILED"}
            tr.error = {
                "code": "DISPATCH_FLOW_CRASH",
                "message": message[:200],
                "retryable": False,
                "category": "SYSTEM",
            }
        finally:
            if acquired_lock and lock_uri:
                self._lock_mgr.release(lock_uri, task_id)
            # Update run status even after callback or flow exceptions.
            self._update_run_status(run)

    def _send_task_callback(self, run: _RunRecord, tr: _TaskRun, status: str) -> bool:
        url_tmpl = run.callback_task_status_url
        if not url_tmpl:
            return True
        url = url_tmpl.replace("{task_id}", tr.task_id)

        payload = {
            "run_id": run.run_id,
            "plan_id": run.plan_id,
            "task_id": tr.task_id,
            "external_task_id": tr.task_id,
            "executor_id": self.executor_id,
            "status": status,
            "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_ms": tr.duration_ms,
            "result": tr.result,
            "error": tr.error,
            "artifacts": tr.artifacts,
        }
        return self._callback._send(url, payload, run.callback_auth_token)

    def _update_run_status(self, run: _RunRecord):
        tasks = list(run.tasks.values())
        all_done = all(t.status in (
            TaskRunStatus.SUCCEEDED, TaskRunStatus.FAILED,
            TaskRunStatus.TIMEOUT, TaskRunStatus.CANCELED,
            TaskRunStatus.CALLBACK_FAILED, TaskRunStatus.SKIPPED,
        ) for t in tasks)
        if not all_done:
            return

        run.finished_at = time.time()
        has_failed = any(t.status in (
            TaskRunStatus.FAILED, TaskRunStatus.TIMEOUT, TaskRunStatus.CALLBACK_FAILED,
        ) for t in tasks)
        has_succeeded = any(t.status == TaskRunStatus.SUCCEEDED for t in tasks)

        if has_failed and has_succeeded:
            run.status = RunStatus.PARTIAL_FAILED
        elif has_failed and not has_succeeded:
            run.status = RunStatus.FAILED
        else:
            run.status = RunStatus.SUCCEEDED


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
