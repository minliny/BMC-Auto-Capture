"""
PlanGenerator — cross-product devices × tasks, filter by group + multi-tag matching.
"""


from __future__ import annotations
import logging

from ..models.device import Device
from ..models.task import Task
from ..models.task_plan import TaskPlan

logger = logging.getLogger("bmc_auto_capture.planner")


def generate_plans(devices: list[Device], tasks: list[Task]) -> list[TaskPlan]:
    """Match enabled devices to enabled tasks by group + tags."""
    enabled_devices = [d for d in devices if d.enabled]
    enabled_tasks = [t for t in tasks if t.enabled]

    plans: list[TaskPlan] = []

    for device in enabled_devices:
        for task in enabled_tasks:
            if _matches(device, task):
                plans.append(TaskPlan(device=device, task=task))

    # Sort: by device_group, then device_name, then task.sequence
    plans.sort(key=lambda p: (
        p.device.device_group,
        p.device.device_name,
        p.task.sequence,
    ))

    total_devices = len({p.device.device_name for p in plans})
    logger.info(
        "计划生成:  %d devices × %d tasks = %d plans (%d unique devices)",
        len(enabled_devices), len(enabled_tasks), len(plans), total_devices,
    )

    return plans


def _matches(device: Device, task: Task) -> bool:
    """Check if a task should run on a device."""
    # Group matching (case-insensitive)
    if task.match_group:
        if device.device_group.lower() != task.match_group.lower():
            return False
    return True
