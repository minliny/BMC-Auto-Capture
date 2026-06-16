"""Job payload construction for real plan execution."""

from __future__ import annotations

from typing import Any

from .models import PlanRunItem


class PlanRunJobPayloadBuilder:
    """Build RealRunner-compatible payloads from expanded plan items."""

    def build(self, item: PlanRunItem) -> dict[str, Any]:
        device = item._device
        task = item._task

        if device is None or task is None:
            raise ValueError("Missing device or task reference")

        bmc_ip = (getattr(device, "bmc_ip", "") or "").strip()
        inband_ip = (getattr(device, "inband_ip", "") or "").strip()
        task_type = getattr(task, "task_type", "")
        exec_mode = getattr(task, "execution_mode", "")
        cmd = getattr(task, "command_or_url", "") or ""
        raw_cmd = cmd
        task_id = item.task_id or getattr(task, "task_id", "") or item.task_name
        plan_item_id = item.plan_item_id or f"{item.plan_id}:{item.device_name}:{task_id}"

        try:
            per_group_commands = getattr(task, "_per_group_commands", None) or {}
            if per_group_commands and item.device_group.upper() in per_group_commands:
                cmd = per_group_commands[item.device_group.upper()]
        except Exception:
            pass

        device_group = (getattr(device, "device_group", "") or "").upper()
        ssh_type = "SSH_VRP" if device_group in ("L1", "L2") else "SSH_LINUX"
        ssh_profile = "vrp" if ssh_type == "SSH_VRP" else "linux"

        bmc_user = getattr(device, "bmc_username", "") or ""
        bmc_pass = getattr(device, "bmc_password", "") or ""
        ssh_user = getattr(device, "inband_username", "") or ""
        ssh_pass = getattr(device, "inband_password", "") or ""

        oob_ref = bmc_pass if bmc_pass.startswith("env:") or bmc_pass.startswith("secret:") else ""
        inband_ref = ssh_pass if ssh_pass.startswith("env:") or ssh_pass.startswith("secret:") else ""
        if bmc_pass and not oob_ref:
            oob_ref = bmc_pass
        if ssh_pass and not inband_ref:
            inband_ref = ssh_pass

        device_snapshot = {
            "device_name": item.device_name,
            "device_group": item.device_group,
            "oob_ip": bmc_ip,
            "oob_port": 443,
            "oob_username": bmc_user,
            "oob_password_ref": oob_ref,
            "inband_ip": inband_ip,
            "inband_port": 22,
            "inband_username": ssh_user,
            "inband_password_ref": inband_ref,
            "ssh_type": ssh_type,
        }

        task_snapshot = {
            "plan_id": str(item.plan_id),
            "task_id": task_id,
            "plan_item_id": plan_item_id,
            "task_name": item.task_name,
            "sequence": int(getattr(task, "sequence", 0) or 0),
            "sequence_str": str(getattr(task, "sequence_str", "") or ""),
            "task_type": task_type,
            "execution_mode": exec_mode,
            "match_group": getattr(task, "match_group", "") or "",
            "url": cmd,
            "command_or_url": cmd,
            "raw_command_or_url": raw_cmd,
            "ssh_cmd": cmd,
            "actions_json": getattr(task, "actions_json", "") or "",
            "rules_json": getattr(task, "rules_json", "") or "",
            "timeout_seconds": int(getattr(task, "timeout_seconds", 60) or 60),
            "retry_count": int(getattr(task, "retry_count", 0) or 0),
            "output_dir_template": (
                getattr(task, "output_dir_template", "{device_name}/{task_name}")
                or "{device_name}/{task_name}"
            ),
            "image_name_template": (
                getattr(task, "image_name_template", "{device_name}_{task_name}_{step}_{timestamp}")
                or "{device_name}_{task_name}_{step}_{timestamp}"
            ),
            "full_screenshot": bool(getattr(task, "full_screenshot", False)),
            "screenshot_mode": getattr(task, "screenshot_mode", "auto") or "auto",
        }
        if task_type.upper() in ("SSH", "TELNET") or exec_mode.upper() == "SSH_CMD":
            task_snapshot["ssh_profile"] = ssh_profile
            task_snapshot["ssh_evidence_mode"] = "terminal"

        task_def = getattr(task, "_task_def", None) or {}
        if isinstance(task_def, dict) and task_def:
            task_snapshot["task_def"] = task_def
        per_group_commands = getattr(task, "_per_group_commands", None) or {}
        if isinstance(per_group_commands, dict) and per_group_commands:
            task_snapshot["per_group_commands"] = per_group_commands
        per_group_no_split = getattr(task, "_per_group_no_split", None) or {}
        if isinstance(per_group_no_split, dict) and per_group_no_split:
            task_snapshot["per_group_no_split"] = per_group_no_split
        per_group_timeout_seconds = getattr(task, "_per_group_timeout_seconds", None) or {}
        if isinstance(per_group_timeout_seconds, dict) and per_group_timeout_seconds:
            task_snapshot["per_group_timeout_seconds"] = per_group_timeout_seconds
        if getattr(task, "_no_split", False):
            task_snapshot["no_split"] = True

        return {
            "job_id": plan_item_id,
            "plan_id": str(item.plan_id),
            "device_snapshot": device_snapshot,
            "task_snapshot": task_snapshot,
        }
