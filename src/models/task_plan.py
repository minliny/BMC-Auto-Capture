"""
TaskPlan — the minimal scheduling unit: one device × one task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid

from .device import Device
from .task import Task


def make_plan_item_id(plan_id: str, device_id: str, task_id: str) -> str:
    """Build the stable id for one device-task execution inside a plan batch."""
    return f"{plan_id}:{device_id}:{task_id}"


@dataclass
class TaskPlan:
    device: Device
    task: Task
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    plan_item_id: str = ""
    client_task_id: str = ""
    status: str = "PENDING"
    skip_reason: str = ""
    retry_attempt: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    # Timing — set by scheduler/executor lifecycle
    ready_at: float = 0.0
    resource_wait_started_at: float = 0.0
    resource_acquired_at: float = 0.0
    executor_started_at: float = 0.0
    executor_finished_at: float = 0.0
    ended_at: float = 0.0

    # Resource lease tracking
    # _resource_lease_held=True means the scheduler already acquired the
    # Global ResourceRegistry lease for this plan's endpoint_key.
    # When False, the executor MUST self-acquire as a fallback safety net.
    _resource_lease_held: bool = False
    _execution_id: str = ""

    @property
    def device_id(self) -> str:
        return self.device.device_name

    @property
    def endpoint_type(self) -> str:
        t = self.task.task_type.upper()
        if t in ("BMC",):
            return "BMC"
        if t in ("SSH", "TELNET"):
            return "INBAND"
        return "UNKNOWN"

    @property
    def endpoint_key(self) -> str:
        if self.endpoint_type == "BMC":
            ip = self.device.bmc_ip
            if not ip:
                return f"BMC:MISSING_IP:{self.device.device_name}"
            return f"BMC:{ip}:443"
        elif self.endpoint_type == "INBAND":
            ip = self.device.inband_ip
            if not ip:
                return f"INBAND:MISSING_IP:{self.device.device_name}"
            t = self.task.task_type.upper()
            port = "23" if t == "TELNET" else "22"
            return f"INBAND:{ip}:{port}"
        return f"UNKNOWN:{self.device.device_name}"

    @property
    def effective_task_id(self) -> str:
        return self.task_id or self.task.effective_task_id

    @property
    def effective_plan_item_id(self) -> str:
        if self.plan_item_id:
            return self.plan_item_id
        return make_plan_item_id(self.plan_id, self.device_id, self.effective_task_id)

    @property
    def protocol(self) -> str:
        t = self.task.task_type.upper()
        if t in ("BMC",):
            return "BMC"
        if t in ("SSH", "TELNET"):
            return "SSH"
        return "UNKNOWN"

    # Convenience aliases matching endpoint_type names used in scheduling
    @property
    def resource_type(self) -> str:
        return self.endpoint_type

    # --- Duration helpers ---
    @property
    def duration_seconds(self) -> float:
        if self.ended_at and self.ready_at:
            return self.ended_at - self.ready_at
        return 0.0

    @property
    def resource_wait_seconds(self) -> float:
        if self.resource_acquired_at and self.resource_wait_started_at:
            return self.resource_acquired_at - self.resource_wait_started_at
        return 0.0

    @property
    def executor_duration_seconds(self) -> float:
        if self.executor_finished_at and self.executor_started_at:
            return self.executor_finished_at - self.executor_started_at
        return 0.0

    # --- API v0.1 compat ---

    @property
    def attempt(self) -> int:
        """Alias for retry_attempt — API v0.1 Job model field name."""
        return self.retry_attempt

    @attempt.setter
    def attempt(self, value: int) -> None:
        self.retry_attempt = value

    @property
    def lock_uri(self) -> str:
        """Derive lock_uri from device + task type. Never falls back to device_name."""
        from ..api_models.lock_uri import derive_lock_uri_from_device
        return derive_lock_uri_from_device(
            self.device,
            execution_mode=self.task.execution_mode,
            ssh_type=self.device.ssh_type,
        )
