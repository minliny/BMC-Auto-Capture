"""
DirectDispatchService — orchestrates job receive → execute → callback flow.

Thread-safe. Uses:
  - DirectDispatchStore for job state
  - ResourceLockManager for local concurrency control
  - FakeRunner for execution (swappable)
  - ServerCallbackClient + HttpCallbackTransport or FakeCallbackTransport
"""

from __future__ import annotations
import logging
import os
import threading
import time
import uuid
from typing import Any

from ..direct_dispatch_store import DirectDispatchStore, StoredJob, JobStoreStatus
from ..job_runner_adapter import FakeRunner, RealRunnerAdapter, JobResult
from ..resource_lock_manager import ResourceLockManager
from ..server_callback_client import (
    ServerCallbackClient,
    FakeCallbackTransport,
    HttpCallbackTransport,
)
from ..api_models.lock_uri import derive_lock_uri, LockUriDerivationError
from ..plan_item_status_callback_client import validate_callback_url

logger = logging.getLogger("bmc_auto_capture.dispatch")


# ---------------------------------------------------------------------------
# Validation error codes
# ---------------------------------------------------------------------------

class ValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


INVALID_EXTERNAL_TASK_ID = "INVALID_EXTERNAL_TASK_ID"
INVALID_CALLBACK_URL = "INVALID_CALLBACK_URL"
INVALID_TASK_SNAPSHOT = "INVALID_TASK_SNAPSHOT"
MISSING_LOCK_URI = "MISSING_LOCK_URI"
DUPLICATE_COMMAND = "DUPLICATE_COMMAND"
MISSING_COMMAND_ID = "MISSING_COMMAND_ID"
REAL_RUNNER_NOT_ENABLED = "REAL_RUNNER_NOT_ENABLED"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class DirectDispatchService:
    """Orchestrates the full dispatch→execute→callback lifecycle.

    Execution model: submit_job() stores job and returns immediately.
    Call run_pending_once() to process one pending job synchronously,
    or use the background thread via start_background_worker().

    Lock behavior: before execution, acquires lock_uri via ResourceLockManager.
    If lock is held by another job, the job stays ACCEPTED and is skipped
    for this cycle (will be retried on next run_pending).
    """

    def __init__(
        self,
        executor_id: str = "exec-win-001",
        store: DirectDispatchStore | None = None,
        runner: Any = None,  # JobRunner protocol
        lock_manager: ResourceLockManager | None = None,
        callback_client: ServerCallbackClient | None = None,
        callback_transport: Any = None,
        use_http_callback: bool = False,
        callback_timeout_seconds: float = 30.0,
        runner_mode: str = "fake",
        output_root: str = "./output_api_direct",
        allow_real_runner: bool = False,
    ):
        self.executor_id = executor_id
        self._store = store or DirectDispatchStore()
        self._lock_mgr = lock_manager or ResourceLockManager()

        # Runner selection
        if runner is not None:
            self._runner = runner
        elif runner_mode == "real":
            if not (allow_real_runner or _env_truthy("EXECUTOR_ENABLE_REAL_RUNNER")):
                raise ValueError(
                    "runner_mode=real requires DirectDispatchService(allow_real_runner=True) "
                    "or EXECUTOR_ENABLE_REAL_RUNNER=1"
                )
            self._runner = RealRunnerAdapter(output_root=output_root)
        else:
            self._runner = FakeRunner()

        # Transport selection
        if callback_transport is not None:
            self._transport = callback_transport
        elif use_http_callback:
            self._transport = HttpCallbackTransport(timeout_seconds=callback_timeout_seconds)
        else:
            self._transport = FakeCallbackTransport()

        self._callback = callback_client or ServerCallbackClient(
            executor_id=executor_id,
            transport=self._transport,
        )
        self._use_http = use_http_callback
        self._started_at = time.time()
        self._pending_queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None
        self._stop_bg = threading.Event()

    @property
    def store(self) -> DirectDispatchStore:
        return self._store

    @property
    def transport(self) -> Any:
        return self._transport

    @property
    def lock_manager(self) -> ResourceLockManager:
        return self._lock_mgr

    @property
    def callback_client(self) -> ServerCallbackClient:
        return self._callback

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def submit_job(self, request_dict: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a dispatched job. Returns accept response dict."""
        command_id = request_dict.get("command_id", "")
        external_task_id = request_dict.get("external_task_id", "")
        callback = request_dict.get("callback", {})
        job_payload = request_dict.get("job", {})

        # --- validation ---
        if not command_id:
            raise ValidationError(MISSING_COMMAND_ID, "command_id is required")
        if not external_task_id:
            raise ValidationError(INVALID_EXTERNAL_TASK_ID, "external_task_id is required")

        status_url = callback.get("status_url", "") if isinstance(callback, dict) else ""
        if not status_url:
            raise ValidationError(INVALID_CALLBACK_URL, "callback.status_url is required")
        ok, reason = validate_callback_url(status_url)
        if not ok:
            raise ValidationError(INVALID_CALLBACK_URL, reason)

        task_snapshot = job_payload.get("task_snapshot", {}) if isinstance(job_payload, dict) else {}
        if not task_snapshot or not isinstance(task_snapshot, dict):
            raise ValidationError(INVALID_TASK_SNAPSHOT, "job.task_snapshot is required")

        # --- derive lock_uri ---
        lock_uri = self._derive_lock_uri_from_payload(request_dict)
        if not lock_uri:
            raise ValidationError(MISSING_LOCK_URI,
                "Cannot derive lock_uri from job payload. "
                "Provide resource_lock.lock_uri or sufficient device_snapshot fields."
            )

        # --- idempotency ---
        if self._store.is_command_duplicate(command_id):
            existing = self._store.get_by_command_id(command_id)
            return {
                "accepted": False,
                "external_task_id": external_task_id,
                "job_id": existing.job_id if existing else "",
                "status": existing.status if existing else "",
                "message": "duplicate command_id",
                "duplicate": True,
            }

        # --- create ---
        job_id = job_payload.get("job_id", "") or f"job-{uuid.uuid4().hex[:12]}"

        stored = StoredJob(
            external_task_id=external_task_id,
            job_id=job_id,
            command_id=command_id,
            status=JobStoreStatus.ACCEPTED,
            callback_status_url=status_url,
            callback_artifact_url=callback.get("artifact_url", "") if isinstance(callback, dict) else "",
            callback_auth_token=callback.get("auth_token", "") if isinstance(callback, dict) else "",
            raw_payload=request_dict,
            lock_uri=lock_uri,
        )
        ok = self._store.create(stored)
        if not ok:
            existing = self._store.get_by_command_id(command_id)
            return {
                "accepted": False,
                "external_task_id": external_task_id,
                "job_id": existing.job_id if existing else "",
                "status": existing.status if existing else "",
                "message": "duplicate command_id (race)",
                "duplicate": True,
            }

        with self._queue_lock:
            self._pending_queue.append(job_id)

        return {
            "accepted": True,
            "external_task_id": external_task_id,
            "job_id": job_id,
            "status": JobStoreStatus.ACCEPTED,
            "message": "job accepted",
            "duplicate": False,
        }

    # ------------------------------------------------------------------
    # Run pending
    # ------------------------------------------------------------------

    def run_pending_once(self) -> bool:
        """Pop one pending job and execute it synchronously. Returns True if processed."""
        with self._queue_lock:
            if not self._pending_queue:
                return False
            job_id = self._pending_queue.pop(0)

        self._execute_and_callback(job_id)
        return True

    def run_all_pending(self) -> int:
        """Execute all pending jobs synchronously. Returns count of processed jobs.

        Jobs blocked by resource lock are re-queued for the next cycle.
        """
        count = 0
        max_cycles = len(self._store) * 2  # prevent infinite loop on locked jobs
        cycle = 0
        while cycle < max_cycles:
            cycle += 1
            with self._queue_lock:
                if not self._pending_queue:
                    break
                job_id = self._pending_queue.pop(0)

            processed = self._execute_and_callback(job_id)
            if processed:
                count += 1
            # If not processed (lock held), re-queue
        return count

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def start_background_worker(self, poll_interval: float = 0.5):
        if self._bg_thread and self._bg_thread.is_alive():
            return
        self._stop_bg.clear()
        self._bg_thread = threading.Thread(
            target=self._bg_loop, args=(poll_interval,),
            daemon=True, name="dispatch-worker",
        )
        self._bg_thread.start()

    def stop_background_worker(self):
        self._stop_bg.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=5.0)

    def _bg_loop(self, poll_interval: float):
        while not self._stop_bg.is_set():
            try:
                self.run_pending_once()
            except Exception:
                logger.exception("Background worker error")
            self._stop_bg.wait(poll_interval)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self._store.get_job(job_id)
        if job is None:
            return {"job_id": job_id, "status": "NOT_FOUND"}
        return {
            "job_id": job.job_id,
            "external_task_id": job.external_task_id,
            "command_id": job.command_id,
            "status": job.status,
            "lock_uri": job.lock_uri,
            "lock_held": self._lock_mgr.is_locked(job.lock_uri) if job.lock_uri else False,
            "received_at": _fmt_ts(job.received_at),
            "started_at": _fmt_ts(job.started_at),
            "finished_at": _fmt_ts(job.finished_at),
            "duration_ms": job.duration_ms,
            "last_callback_error": job.last_callback_error,
            "last_callback_status_code": job.last_callback_status_code,
            "last_callback_at": _fmt_ts(job.last_callback_at),
            "error": job.error,
            "result_summary": job.result_summary,
        }

    def get_executor_status(self) -> dict[str, Any]:
        counts = self._store.status_counts()
        lock_snap = self._lock_mgr.snapshot()
        return {
            "executor_id": self.executor_id,
            "status": "ONLINE",
            "version": "0.2.4",
            "job_counts": counts,
            "total_jobs": len(self._store),
            "active_locks": len(lock_snap),
            "uptime_seconds": self.uptime_seconds,
        }

    # ------------------------------------------------------------------
    # Internal: lock_uri derivation
    # ------------------------------------------------------------------

    def _derive_lock_uri_from_payload(self, request_dict: dict[str, Any]) -> str:
        """Extract or derive lock_uri from the job payload."""
        job_payload = request_dict.get("job", {})
        resource_lock = job_payload.get("resource_lock", {})

        # Explicit lock_uri from resource_lock
        explicit = resource_lock.get("lock_uri", "") if isinstance(resource_lock, dict) else ""
        if explicit:
            return explicit

        # Derive from device_snapshot + task_snapshot
        device_snapshot = job_payload.get("device_snapshot", {})
        task_snapshot = job_payload.get("task_snapshot", {})

        oob_ip = device_snapshot.get("oob_ip", "")
        inband_ip = device_snapshot.get("inband_ip", "")
        execution_mode = task_snapshot.get("execution_mode", "")
        task_type = task_snapshot.get("task_type", "")

        # Determine ssh_type from device_snapshot
        ssh_type = device_snapshot.get("ssh_type", "")
        if not ssh_type:
            device_group = device_snapshot.get("device_group", "").upper()
            if device_group in ("L1", "L2"):
                ssh_type = "SSH_VRP"

        try:
            return derive_lock_uri(
                oob_ip=oob_ip,
                inband_ip=inband_ip,
                execution_mode=execution_mode or task_type,
                ssh_type=ssh_type,
            )
        except LockUriDerivationError:
            return ""

    # ------------------------------------------------------------------
    # Internal: execute + callback
    # ------------------------------------------------------------------

    def _execute_and_callback(self, job_id: str) -> bool:
        """Execute one job with lock protection. Returns True if processed."""
        job = self._store.get_job(job_id)
        if job is None:
            return False

        lock_uri = job.lock_uri

        # --- Acquire lock ---
        if lock_uri:
            if not self._lock_mgr.acquire(lock_uri, job_id):
                # Lock held by another job — re-queue and skip for now
                with self._queue_lock:
                    self._pending_queue.append(job_id)
                return False

        # --- Mark RUNNING ---
        self._store.mark_running(job_id)

        # Callback: started
        if job.callback_status_url:
            self._callback.callback_job_started(
                external_task_id=job.external_task_id,
                job_id=job_id,
                status_url=job.callback_status_url,
                auth_token=job.callback_auth_token,
            )

        # --- Execute ---
        try:
            result = self._runner.run_job(job.raw_payload.get("job", {}))
        except Exception as e:
            logger.exception("Runner crashed for job %s", job_id)
            result = JobResult(
                status="FAILED",
                duration_ms=0,
                error={"code": "RUNNER_CRASH", "message": str(e), "retryable": False, "category": "SYSTEM"},
            )

        # --- Mark finished ---
        status = result.status
        self._store.mark_finished(
            job_id=job_id,
            status=status,
            duration_ms=result.duration_ms,
            error=result.error,
            result_summary={
                "summary": f"EXEC_{status}",
                "steps_total": len(result.steps),
                "steps_success": sum(1 for s in result.steps if s.get("status") == "SUCCEEDED"),
                "steps_failed": sum(1 for s in result.steps if s.get("status") == "FAILED"),
            },
            artifacts=result.artifacts,
        )

        # --- Callback ---
        sent = False
        if job.callback_status_url:
            if result.status == "SUCCEEDED":
                sent = self._callback.callback_job_finished(
                    external_task_id=job.external_task_id,
                    job_id=job_id,
                    status_url=job.callback_status_url,
                    result={
                        "summary": f"EXEC_{result.status}",
                        "steps_total": len(result.steps),
                        "steps_success": sum(1 for s in result.steps if s.get("status") == "SUCCEEDED"),
                        "steps_failed": sum(1 for s in result.steps if s.get("status") == "FAILED"),
                    },
                    duration_ms=result.duration_ms,
                    artifacts=result.artifacts,
                    auth_token=job.callback_auth_token,
                )
            else:
                sent = self._callback.callback_job_failed(
                    external_task_id=job.external_task_id,
                    job_id=job_id,
                    status_url=job.callback_status_url,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    error=result.error,
                    auth_token=job.callback_auth_token,
                )

        if not sent and job.callback_status_url:
            self._store.mark_callback_failed(
                job_id,
                f"Callback returned non-2xx or exception "
                f"(transport={'http' if self._use_http else 'fake'})",
            )

        # --- Release lock ---
        if lock_uri:
            self._lock_mgr.release(lock_uri, job_id)

        return True


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
