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

    # Log disabled items so users know what was skipped
    disabled_devices = [d for d in devices if not d.enabled]
    disabled_tasks = [t for t in tasks if not t.enabled]
    if disabled_devices:
        logger.info("已禁用设备 (%d): %s",
                    len(disabled_devices),
                    ", ".join(d.device_name for d in disabled_devices))
    if disabled_tasks:
        logger.info("已禁用任务 (%d): %s",
                    len(disabled_tasks),
                    ", ".join(t.task_name for t in disabled_tasks))

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

    # Warn about tasks that matched zero devices
    matched_task_names = {p.task.task_name for p in plans}
    unmatched = [t for t in enabled_tasks if t.task_name not in matched_task_names]
    if unmatched:
        logger.warning(
            "未匹配到任何设备的任务 (%d): %s",
            len(unmatched),
            ", ".join(f"{t.task_name}(group={t.match_group})" for t in unmatched),
        )

    return plans


def _matches(device: Device, task: Task) -> bool:
    """Check if a task should run on a device.

    Group matching is case-insensitive and supports multi-group notation:
      "L1/L2"  → matches device_group "L1" or "L2"
      "A3"     → matches device_group "A3" only
    """
    if not task.match_group:
        return True

    device_group = device.device_group.lower()
    # Split on "/" for multi-group tasks
    task_groups = [g.strip().lower() for g in task.match_group.split("/") if g.strip()]
    return device_group in task_groups
