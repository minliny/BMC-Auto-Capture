"""
TaskPlan — the minimal scheduling unit: one device × one task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid

from .device import Device
from .task import Task


@dataclass
class TaskPlan:
    device: Device
    task: Task
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    client_task_id: str = ""
    status: str = "PENDING"
    skip_reason: str = ""
    retry_attempt: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    @property
    def device_id(self) -> str:
        return self.device.device_name

    @property
    def effective_task_id(self) -> str:
        return self.task_id or self.task.task_name

    @property
    def protocol(self) -> str:
        t = self.task.task_type.upper()
        if t in ("BMC",):
            return "BMC"
        if t in ("SSH", "TELNET"):
            return "SSH"
        return "UNKNOWN"
