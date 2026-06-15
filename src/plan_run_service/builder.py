"""Build plan-run items from devices and tasks."""

from __future__ import annotations

from typing import Any, Callable


def derive_lock_uri(device: Any, task: Any) -> str:
    bmc_ip = (getattr(device, "bmc_ip", "") or "").strip()
    inband_ip = (getattr(device, "inband_ip", "") or "").strip()
    task_type = (getattr(task, "task_type", "") or "").upper()
    execution_mode = (getattr(task, "execution_mode", "") or "").upper()
    if task_type in ("BMC",) or execution_mode in ("BMC_URL", "BMC_ACTIONS"):
        return f"bmc://{bmc_ip}" if bmc_ip else ""
    if task_type in ("SSH", "TELNET") or execution_mode in ("SSH_CMD",):
        if not inband_ip:
            return ""
        device_group = (getattr(device, "device_group", "") or "").upper()
        if device_group in ("L1", "L2"):
            return f"ssh-vrp://{inband_ip}"
        return f"ssh-linux://{inband_ip}"
    return ""


class PlanRunBuilder:
    """Expands enabled devices and tasks into plan-run items."""

    def build_items(
        self,
        plan_id: int | str,
        devices: list[Any],
        tasks: list[Any],
        *,
        item_factory: Callable[..., Any],
    ) -> list[Any]:
        enabled_devices = [device for device in devices if getattr(device, "enabled", True)]
        enabled_tasks = [task for task in tasks if getattr(task, "enabled", True)]

        items: list[Any] = []
        for device in enabled_devices:
            for task in enabled_tasks:
                match_group = getattr(task, "match_group", "") or ""
                device_group = getattr(device, "device_group", "") or ""
                if match_group:
                    allowed_groups = [group.strip().upper() for group in match_group.split("/") if group.strip()]
                    if device_group.upper() not in allowed_groups:
                        continue
                items.append(item_factory(
                    plan_id=plan_id,
                    device_name=getattr(device, "device_name", ""),
                    task_name=getattr(task, "task_name", ""),
                    device_group=device_group,
                    task_type=getattr(task, "task_type", ""),
                    execution_mode=getattr(task, "execution_mode", ""),
                    lock_uri=derive_lock_uri(device, task),
                    _device=device,
                    _task=task,
                ))
        return items
