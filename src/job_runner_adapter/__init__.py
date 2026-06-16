"""
Job runner adapter — unified interface for executing API Jobs.

Two modes:
  A. FakeRunner — dry-run for tests, simulates success/failure/timeout.
  B. RealRunnerAdapter — converts API models to existing TaskPlan/Device
     and delegates to BMCExecutor/SSHExecutor.
"""

from __future__ import annotations
import json
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
    execution_result: Any | None = None


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
        bmc_artifact_profile: str = "full",
        ssh_connect_timeout: float = 15.0,
        ssh_command_timeout: float = 60.0,
        ssh_idle_timeout: float = 5.0,
    ):
        self.output_root = output_root
        self._bmc_connect_timeout = bmc_connect_timeout
        self._bmc_page_timeout = bmc_page_timeout
        self._bmc_artifact_profile = bmc_artifact_profile
        self._ssh_connect_timeout = ssh_connect_timeout
        self._ssh_command_timeout = ssh_command_timeout
        self._ssh_idle_timeout = ssh_idle_timeout
        self._bm: Any = None  # BrowserManager, lazy init

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_job(self, job_payload: dict[str, Any]) -> JobResult:
        """Execute a job from the API payload. Routes to BMC or SSH executor."""
        plan, execution_mode, task_type = self._plan_from_job_payload(job_payload)

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

    def run_bmc_session_group(self, job_payloads: list[dict[str, Any]]) -> list[JobResult]:
        """Execute same-endpoint BMC jobs with one browser login/session.

        Tests and custom integrations sometimes monkeypatch ``run_job``.  In that
        case this method deliberately preserves the old per-job adapter path.
        """
        if not job_payloads:
            return []
        if type(self).run_job is not _ORIGINAL_REAL_RUN_JOB:
            return [self.run_job(payload) for payload in job_payloads]

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            plans = []
            seen_plan_ids: set[str] = set()
            for payload in job_payloads:
                plan, execution_mode, task_type = self._plan_from_job_payload(payload)
                if execution_mode not in ("BMC_URL", "BMC_ACTIONS") and task_type not in (
                    "BMC", "BMC_URL", "BMC_ACTIONS",
                ):
                    return [self.run_job(item_payload) for item_payload in job_payloads]
                if not plan.plan_id or plan.plan_id == "api-unknown" or plan.plan_id in seen_plan_ids:
                    plan.plan_id = f"api-group-{len(plans) + 1}"
                seen_plan_ids.add(plan.plan_id)
                plans.append(plan)

            endpoint_keys = {plan.endpoint_key for plan in plans}
            if len(endpoint_keys) != 1:
                return [self.run_job(payload) for payload in job_payloads]

            os.makedirs(self.output_root, exist_ok=True)
            from ..scheduler.bmc_session_runner import BMCEndpointSessionRunner

            runner = BMCEndpointSessionRunner(
                browser_manager=self._get_browser_manager(),
                endpoint_key=plans[0].endpoint_key,
                plans=plans,
                output_root=self.output_root,
                connect_timeout=self._bmc_connect_timeout,
                page_timeout=self._bmc_page_timeout,
                artifact_profile=self._bmc_artifact_profile,
            )
            execution_results = runner.run()
            return [
                self._execution_result_to_job_result(er, started, protocol="BMC")
                for er in execution_results
            ]
        except Exception as e:
            logger.exception("BMC session group execution crashed")
            finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return [
                JobResult(
                    status="FAILED",
                    started_at=started,
                    finished_at=finished,
                    error={
                        "code": "BMC_SESSION_GROUP_CRASH",
                        "message": str(e)[:200],
                        "retryable": True,
                        "category": "BMC",
                    },
                )
                for _ in job_payloads
            ]

    def _plan_from_job_payload(self, job_payload: dict[str, Any]):
        """Convert a public job payload into an internal TaskPlan."""
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
        plan_id = str(
            job_payload.get("plan_id")
            or task_snapshot.get("plan_id")
            or f"api-{job_payload.get('job_id', 'unknown')}"
        )
        task_id = str(task_snapshot.get("task_id") or getattr(task, "task_id", "") or task.task_name)
        plan = TaskPlan(
            device=device,
            task=task,
            plan_id=plan_id,
            task_id=task_id,
            plan_item_id=str(task_snapshot.get("plan_item_id") or job_payload.get("job_id") or ""),
        )

        execution_mode = task_snapshot.get("execution_mode", "")
        task_type = task_snapshot.get("task_type", "")
        return plan, execution_mode, task_type

    # ------------------------------------------------------------------
    # BMC execution
    # ------------------------------------------------------------------

    def _run_bmc(self, plan: Any) -> JobResult:
        from ..executor.bmc_executor import BMCExecutor
        from ..executor.retry import execute_with_retry

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.time()

        try:
            executor = BMCExecutor(
                browser_manager=self._get_browser_manager(),
                connect_timeout=self._bmc_connect_timeout,
                page_timeout=self._bmc_page_timeout,
                artifact_profile=self._bmc_artifact_profile,
            )
            exec_result = execute_with_retry(executor, plan, self.output_root)
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

        return self._execution_result_to_job_result(exec_result, started, protocol="BMC")

    # ------------------------------------------------------------------
    # SSH execution
    # ------------------------------------------------------------------

    def _run_ssh(self, plan: Any) -> JobResult:
        from ..executor.ssh_executor import SSHExecutor
        from ..executor.retry import execute_with_retry

        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.time()

        try:
            executor = SSHExecutor(
                connect_timeout=self._ssh_connect_timeout,
                command_timeout=self._ssh_command_timeout,
                idle_timeout=self._ssh_idle_timeout,
            )
            exec_result = execute_with_retry(executor, plan, self.output_root)
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

        return self._execution_result_to_job_result(exec_result, started, protocol="SSH")

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

        task = Task(
            row_index=0,
            sequence=int(snapshot.get("sequence", 0) or 0),
            sequence_str=str(snapshot.get("sequence_str", "") or ""),
            task_name=snapshot.get("task_name", ""),
            task_type=snapshot.get("task_type", "BMC"),
            execution_mode=snapshot.get("execution_mode", "BMC_URL"),
            match_group=snapshot.get("match_group", ""),
            command_or_url=cmd,
            actions_json=snapshot.get("actions_json", ""),
            rules_json=snapshot.get("rules_json", ""),
            output_dir_template=snapshot.get(
                "output_dir_template", "{device_name}/{task_name}"
            ),
            image_name_template=snapshot.get(
                "image_name_template", "{device_name}_{task_name}_{step}_{timestamp}"
            ),
            timeout_seconds=int(snapshot.get("timeout_seconds", 60)),
            retry_count=int(snapshot.get("retry_count", 0)),
            enabled=True,
            full_screenshot=bool(snapshot.get("full_screenshot", False)),
            screenshot_mode=snapshot.get("screenshot_mode", "auto") or "auto",
            task_id=str(snapshot.get("task_id", "") or ""),
        )
        task_def = self._coerce_dict(snapshot.get("task_def"))
        for key in (
            "ssh_profile",
            "ssh_type",
            "evidence_mode",
            "ssh_evidence_mode",
            "ssh_transport",
            "ssh_strategy",
            "per_group_ssh_profile",
            "per_group_ssh_type",
            "per_group_evidence_mode",
            "per_group_ssh_evidence_mode",
            "per_group_ssh_transport",
            "per_group_ssh_strategy",
            "artifact_profile",
            "bmc_artifact_profile",
        ):
            if key in snapshot and snapshot.get(key) not in ("", None):
                task_def.setdefault(key, snapshot.get(key))
        if task_def:
            object.__setattr__(task, "_task_def", task_def)
        per_group_commands = self._coerce_dict(snapshot.get("per_group_commands"))
        if per_group_commands:
            object.__setattr__(task, "_per_group_commands", per_group_commands)
        per_group_no_split = self._coerce_dict(snapshot.get("per_group_no_split"))
        if per_group_no_split:
            object.__setattr__(task, "_per_group_no_split", per_group_no_split)
        per_group_timeout_seconds = (
            self._coerce_dict(snapshot.get("per_group_timeout_seconds"))
            or self._coerce_dict(snapshot.get("per_group_timeout"))
        )
        if per_group_timeout_seconds:
            object.__setattr__(task, "_per_group_timeout_seconds", per_group_timeout_seconds)
        if snapshot.get("no_split"):
            object.__setattr__(task, "_no_split", True)
        return task

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    # ------------------------------------------------------------------
    # Result mapping
    # ------------------------------------------------------------------

    def _execution_result_to_job_result(self, er: Any, started: str, protocol: str = "") -> JobResult:
        status = er.execution_status or ""
        category = protocol or ("BMC" if "BMC" in str(status) else "SSH")

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
                "code": self._map_error_code(status, reason, category),
                "message": reason[:300],
                "retryable": status not in ("EXEC_SKIPPED_PORT_BLOCKED",),
                "category": category,
            }

        return JobResult(
            status=job_status,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            steps=steps,
            artifacts=artifacts,
            error=error,
            execution_result=er,
        )

    @staticmethod
    def _map_error_code(status: str, reason: str, category: str = "") -> str:
        prefix = category or ("BMC" if "BMC" in str(status) else "SSH")
        if "Retry wrapper exception" in reason:
            return f"{prefix}_EXECUTOR_CRASH"
        if "timeout" in reason.lower() or status == "EXEC_TIMEOUT":
            return f"{prefix}_TIMEOUT"
        if "auth" in reason.lower() or "password" in reason.lower():
            return f"{prefix}_AUTH_FAILED"
        if "connect" in reason.lower() or "refused" in reason.lower():
            return f"{prefix}_CONNECT_FAILED"
        if "port" in reason.lower() or "blocked" in reason.lower():
            return "BMC_CONNECT_FAILED"
        return f"{prefix}_EXEC_ERROR"

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


_ORIGINAL_REAL_RUN_JOB = RealRunnerAdapter.run_job
