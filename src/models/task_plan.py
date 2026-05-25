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
    status: str = "PENDING"
    retry_attempt: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    @property
    def device_id(self) -> str:
        return self.device.device_name

    @property
    def task_id(self) -> str:
        return self.task.task_name

    @property
    def protocol(self) -> str:
        t = self.task.task_type.upper()
        if t in ("BMC",):
            return "BMC"
        if t in ("SSH", "TELNET"):
            return "SSH"
        return "UNKNOWN"
