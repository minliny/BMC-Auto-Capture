"""
Job runner adapter — unified interface for executing API Jobs.

Two modes:
  A. FakeRunner — dry-run for tests, simulates success/failure/timeout.
  B. RealRunnerAdapter — converts API models to existing TaskPlan/Device
     and delegates to BMCExecutor/SSHExecutor.
"""

from __future__ import annotations
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger("bmc_auto_capture.runner_adapter")


@dataclass
class JobResult:
    """Unified result from any runner."""
    status: str = ""           # SUCCEEDED | FAILED | TIMEOUT
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None


class JobRunner(Protocol):
    """Protocol for any job runner implementation."""
    def run_job(self, job_payload: dict[str, Any]) -> JobResult: ...


# ===========================================================================
# FakeRunner — dry-run for tests
# ===========================================================================

class FakeRunner:
    """Dry-run runner for testing. Controlled by _fake_* keys in job_payload."""

    DEFAULT_DURATION_MS = 100

    def run_job(self, job_payload: dict[str, Any]) -> JobResult:
        now = datetime.now(timezone.utc)
        started = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        mode = job_payload.get("_fake_result", "success")
        duration = int(job_payload.get("_fake_duration_ms", self.DEFAULT_DURATION_MS))
        error_code = job_payload.get("_fake_error_code", "FAKE_ERROR")

        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if mode == "success":
            return JobResult(
                status="SUCCEEDED", started_at=started, finished_at=finished,
                duration_ms=duration,
                steps=[
                    {"step_index": 0, "step_name": "fake_step", "status": "SUCCEEDED", "duration_ms": duration},
                ], error=None,
            )
        elif mode == "timeout":
            return JobResult(
                status="TIMEOUT", started_at=started, finished_at=finished,
                duration_ms=duration,
                error={"code": error_code or "TIMEOUT", "message": f"Fake timeout {duration}ms",
                       "retryable": True, "category": "SYSTEM"},
            )
        else:
            return JobResult(
                status="FAILED", started_at=started, finished_at=finished,
                duration_ms=duration,
                steps=[
                    {"step_index": 0, "step_name": "fake_step", "status": "FAILED", "duration_ms": duration},
                ],
                error={"code": error_code or "FAKE_ERROR", "message": f"Fake failure: {error_code}",
                       "retryable": True, "category": "BMC"},
            )


# ===========================================================================
# RealRunnerAdapter — real BMC/SSH execution
# ===========================================================================

class UnsupportedTaskTypeError(ValueError):
    """Raised when task_type/execution_mode is not supported by RealRunnerAdapter."""


class RealRunnerAdapter:
    """Converts API Job payloads to existing TaskPlan/Device and executes via
    BMCExecutor / SSHExecutor.

    Requires:
      - secret_resolver for password_ref → actual password
      - BrowserManager for BMC tasks (created lazily)
    """

    def __init__(
        self,
        output_root: str = "./output_api_direct",
        bmc_connect_timeout: float = 30.0,
        bmc_page_timeout: float = 60.0,
        ssh_connect_timeout: float = 15.0,
        ssh_command_timeout: float = 60.0,
        ssh_idle_timeout: float = 5.0,
    ):
        self.output_root = output_root
        self._bmc_connect_timeout = bmc_connect_timeout
        self._bmc_page_timeout = bmc_page_timeout
        self._ssh_connect_timeout = ssh_connect_timeout
        self._ssh_command_timeout = ssh_command_timeout
        self._ssh_idle_timeout = ssh_idle_timeout
        self._bm: Any = None  # BrowserManager, lazy init

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_job(self, job_payload: dict[str, Any]) -> JobResult:
        """Execute a job from the API payload. Routes to BMC or SSH executor."""
        from ..secret_resolver import resolve_secrets

        device_snapshot = job_payload.get("device_snapshot", {})
        task_snapshot = job_payload.get("task_snapshot", {})

        # Resolve secrets
        secrets = resolve_secrets(device_snapshot)
        oob_password = secrets.get("oob_password", "")
        inband_password = secrets.get("inband_password", "")

        # Build Device
        device = self._device_from_snapshot(device_snapshot, oob_password, inband_password)

        # Build Task
        task = self._task_from_snapshot(task_snapshot)

        # Build TaskPlan
        from ..models.task_plan import TaskPlan
        plan = TaskPlan(device=device, task=task)
        plan.plan_id = f"api-{job_payload.get('job_id', 'unknown')}"

        # Route
        execution_mode = task_snapshot.get("execution_mode", "")
        task_type = task_snapshot.get("task_type", "")

        os.makedirs(self.output_root, exist_ok=True)

        if execution_mode in ("BMC_URL", "BMC_ACTIONS") or task_type in ("BMC", "BMC_URL", "BMC_ACTIONS"):
            return self._run_bmc(plan)
        elif execution_mode == "SSH_CMD" or task_type in ("SSH", "SSH_CMD", "TELNET"):
            return self._run_ssh(plan)
        else:
            return JobResult(
                status="FAILED",
                error={
                    "code": "UNSUPPORTED_TASK_TYPE",
                    "message": f"Unsupported execution_mode={execution_mode} task_type={task_type}",
                    "retryable": False,
                    "category": "CONFIG",
                },
            )

    # ------------------------------------------------------------------
    # BMC execution
    # ------------------------------------------------------------------

    def _run_bmc(self, plan: Any) -> JobResult:
        from ..executor.bmc_executor import BMCExecutor

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.time()

        try:
            exec_result = BMCExecutor(
                browser_manager=self._get_browser_manager(),
                connect_timeout=self._bmc_connect_timeout,
                page_timeout=self._bmc_page_timeout,
            ).execute(plan, self.output_root)
        except Exception as e:
            logger.exception("BMC execution crashed for plan %s", plan.plan_id)
            elapsed_ms = int((time.time() - t0) * 1000)
            return JobResult(
                status="FAILED", started_at=started,
                finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_ms=elapsed_ms,
                error={"code": "BMC_EXECUTOR_CRASH", "message": str(e)[:200],
                       "retryable": True, "category": "BMC"},
            )

        return self._execution_result_to_job_result(exec_result, started)

    # ------------------------------------------------------------------
    # SSH execution
    # ------------------------------------------------------------------

    def _run_ssh(self, plan: Any) -> JobResult:
        from ..executor.ssh_executor import SSHExecutor

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.time()

        try:
            exec_result = SSHExecutor(
                connect_timeout=self._ssh_connect_timeout,
                command_timeout=self._ssh_command_timeout,
                idle_timeout=self._ssh_idle_timeout,
            ).execute(plan, self.output_root)
        except Exception as e:
            logger.exception("SSH execution crashed for plan %s", plan.plan_id)
            elapsed_ms = int((time.time() - t0) * 1000)
            return JobResult(
                status="FAILED", started_at=started,
                finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_ms=elapsed_ms,
                error={"code": "SSH_EXECUTOR_CRASH", "message": str(e)[:200],
                       "retryable": True, "category": "SSH"},
            )

        return self._execution_result_to_job_result(exec_result, started)

    # ------------------------------------------------------------------
    # Model conversion
    # ------------------------------------------------------------------

    def _device_from_snapshot(
        self, snapshot: dict[str, Any], oob_password: str = "", inband_password: str = ""
    ) -> Any:
        from ..models.device import Device
        return Device(
            row_index=0,
            device_name=snapshot.get("device_name", ""),
            device_group=snapshot.get("device_group", ""),
            bmc_ip=snapshot.get("oob_ip", ""),
            bmc_username=snapshot.get("oob_username", ""),
            bmc_password=oob_password,
            inband_ip=snapshot.get("inband_ip", ""),
            inband_username=snapshot.get("inband_username", ""),
            inband_password=inband_password,
            enabled=True,
            tags="",
        )

    def _task_from_snapshot(self, snapshot: dict[str, Any]) -> Any:
        from ..models.task import Task

        # SSH: prefer explicit ssh_cmd or command field; fallback to url or command_or_url
        cmd = snapshot.get("ssh_cmd", "") or snapshot.get("command", "") or snapshot.get(
            "command_or_url", ""
        ) or snapshot.get("url", "")

        return Task(
            row_index=0,
            sequence=0,
            task_name=snapshot.get("task_name", ""),
            task_type=snapshot.get("task_type", "BMC"),
            execution_mode=snapshot.get("execution_mode", "BMC_URL"),
            match_group="",
            command_or_url=cmd,
            actions_json=snapshot.get("actions_json", ""),
            rules_json="",
            output_dir_template=snapshot.get(
                "output_dir_template", "{device_name}/{task_name}"
            ),
            image_name_template=snapshot.get(
                "image_name_template", "{device_name}_{task_name}_{step}_{timestamp}"
            ),
            timeout_seconds=int(snapshot.get("timeout_seconds", 60)),
            retry_count=int(snapshot.get("retry_count", 0)),
            enabled=True,
        )

    # ------------------------------------------------------------------
    # Result mapping
    # ------------------------------------------------------------------

    def _execution_result_to_job_result(self, er: Any, started: str) -> JobResult:
        status = er.execution_status or ""

        if status == "EXEC_SUCCESS":
            job_status = "SUCCEEDED"
        elif status in ("EXEC_TIMEOUT",):
            job_status = "TIMEOUT"
        elif status.startswith("EXEC_SKIPPED"):
            job_status = "FAILED"
        else:
            job_status = "FAILED"

        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        duration_ms = int(getattr(er, "duration_seconds", 0) * 1000)

        # Map step results
        steps: list[dict[str, Any]] = []
        for s in getattr(er, "step_results", []) or []:
            steps.append({
                "step_index": getattr(s, "step_index", 0),
                "step_name": getattr(s, "step_name", ""),
                "status": getattr(s, "status", ""),
                "step_type": getattr(s, "step_type", ""),
                "details": (getattr(s, "details", "") or "")[:200],
            })

        # Artifacts metadata (no file upload)
        artifacts: list[dict[str, Any]] = []
        screenshots = getattr(er, "screenshots", ()) or ()
        for ss in screenshots:
            if ss:
                artifacts.append({
                    "artifact_type": "PNG_SCREENSHOT",
                    "relative_path": str(ss),
                    "filename": os.path.basename(str(ss)),
                })
        html_file = getattr(er, "html_file", "")
        if html_file:
            artifacts.append({
                "artifact_type": "HTML_PAGE",
                "relative_path": str(html_file),
                "filename": os.path.basename(str(html_file)),
            })
        txt_file = getattr(er, "txt_file", "")
        if txt_file:
            artifacts.append({
                "artifact_type": "TXT_SSH_OUTPUT",
                "relative_path": str(txt_file),
                "filename": os.path.basename(str(txt_file)),
            })

        # Error
        error = None
        reason = getattr(er, "execution_failure_reason", "")
        if reason and job_status != "SUCCEEDED":
            error = {
                "code": self._map_error_code(status, reason),
                "message": reason[:300],
                "retryable": status not in ("EXEC_SKIPPED_PORT_BLOCKED",),
                "category": "BMC" if "BMC" in str(status) else "SSH",
            }

        return JobResult(
            status=job_status,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            steps=steps,
            artifacts=artifacts,
            error=error,
        )

    @staticmethod
    def _map_error_code(status: str, reason: str) -> str:
        if "timeout" in reason.lower() or status == "EXEC_TIMEOUT":
            return "BMC_TIMEOUT" if "BMC" in str(status) else "SSH_TIMEOUT"
        if "auth" in reason.lower() or "password" in reason.lower():
            return "BMC_AUTH_FAILED" if "BMC" in str(status) else "SSH_AUTH_FAILED"
        if "connect" in reason.lower() or "refused" in reason.lower():
            return "BMC_CONNECT_FAILED" if "BMC" in str(status) else "SSH_CONNECT_FAILED"
        if "port" in reason.lower() or "blocked" in reason.lower():
            return "BMC_CONNECT_FAILED"
        return f"{'BMC' if 'BMC' in str(status) else 'SSH'}_EXEC_ERROR"

    # ------------------------------------------------------------------
    # BrowserManager (lazy)
    # ------------------------------------------------------------------

    def _get_browser_manager(self) -> Any:
        if self._bm is None:
            from ..executor.browser_manager import BrowserManager
            self._bm = BrowserManager(headless=True, max_tasks=200, max_age_seconds=3600)
        return self._bm

    def shutdown(self):
        if self._bm is not None:
            import asyncio
            try:
                asyncio.get_event_loop().run_until_complete(self._bm.teardown())
            except Exception:
                pass
            self._bm = None
