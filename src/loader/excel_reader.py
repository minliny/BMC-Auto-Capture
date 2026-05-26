"""
Excel V2 reader — parses 设备信息 and 任务列表 sheets into Device and Task lists.

Task execution details (URL, commands, rules) are resolved from tasks.json
by matching task_name. Excel columns for execution details are fallbacks only.
"""

from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any
import openpyxl

from ..models.device import Device
from ..models.task import Task

logger = logging.getLogger("bmc_auto_capture.loader")

# Simplified Excel columns for tasks (v2.1):
# 0:任务序号 1:任务名称 2:任务类型 3:设备分组 4:标签
# 5:输出目录模板 6:图片命名格式 7:是否启用
SIMPLIFIED_TASK_COLS = 8


def _str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _bool(val: Any) -> bool:
    s = _str(val).lower()
    if s in ("是", "yes", "true", "1", "y", "启用"):
        return True
    if s in ("否", "no", "false", "0", "n", "禁用", ""):
        return False
    return bool(val)


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _parse_tags(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]
    return tuple(parts)


def load_devices(filepath: str | Path, sheet_name: str = "设备信息") -> list[Device]:
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")

    ws = wb[sheet_name]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    devices: list[Device] = []
    for i, row in enumerate(rows):
        if all(v is None for v in row):
            continue

        vals = [_str(v) for v in row]
        tags = _parse_tags(vals[9]) if len(vals) > 9 else ()

        device = Device(
            row_index=i + 2,
            device_group=vals[0] if len(vals) > 0 else "",
            device_name=vals[1] if len(vals) > 1 else "",
            bmc_ip=vals[2] if len(vals) > 2 else "",
            enabled=_bool(vals[3]) if len(vals) > 3 else True,
            bmc_username=vals[4] if len(vals) > 4 else "",
            bmc_password=vals[5] if len(vals) > 5 else "",
            inband_ip=vals[6] if len(vals) > 6 else "",
            inband_username=vals[7] if len(vals) > 7 else "",
            inband_password=vals[8] if len(vals) > 8 else "",
            tags=tags,
        )
        devices.append(device)

    return devices


def _load_task_defs(tasks_json_path: str | Path | None = None) -> dict[str, dict]:
    """Load task execution definitions from tasks.json."""
    if tasks_json_path is None:
        tasks_json_path = Path(__file__).resolve().parent.parent.parent / "tasks.json"
    if not os.path.exists(str(tasks_json_path)):
        logger.warning("tasks.json not found at %s, using Excel fallback", tasks_json_path)
        return {}

    with open(str(tasks_json_path), "r", encoding="utf-8") as f:
        data = json.load(f)

    tasks_def = data.get("tasks", {})
    logger.info("Loaded %d task definitions from tasks.json", len(tasks_def))
    return tasks_def


def load_tasks(
    filepath: str | Path,
    sheet_name: str = "任务列表",
    tasks_json_path: str | Path | None = None,
) -> list[Task]:
    """Load tasks from Excel, merging with tasks.json definitions by task_name."""
    task_defs = _load_task_defs(tasks_json_path)

    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")

    ws = wb[sheet_name]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    tasks: list[Task] = []
    for i, row in enumerate(rows):
        if all(v is None for v in row):
            continue

        vals = [_str(v) for v in row]
        task_name = vals[1] if len(vals) > 1 else ""

        # Resolve execution details: JSON takes priority, Excel is fallback
        tdef = task_defs.get(task_name, {})

        task_type = tdef.get("task_type") or (vals[2] if len(vals) > 2 else "")
        execution_mode = tdef.get("execution_mode") or ""
        command_or_url = tdef.get("command_or_url") or ""
        timeout_seconds = tdef.get("timeout_seconds", 60)
        retry_count = tdef.get("retry_count", 0)
        rules_json = json.dumps(tdef.get("rules", [])) if tdef.get("rules") else ""

        # Excel columns depend on format:
        # Simplified (8 cols):  seq | name | type | group | tags | outdir | imgname | enabled
        # Full legacy (14 cols): seq | name | type | group | tags | mode | cmd | actions | outdir | imgname | timeout | retry | enabled | rules
        is_simplified = len(vals) <= SIMPLIFIED_TASK_COLS

        if is_simplified:
            match_group = vals[3] if len(vals) > 3 else ""
            match_tags = _parse_tags(vals[4]) if len(vals) > 4 else ()
            output_dir_template = vals[5] if len(vals) > 5 else "{device_group}/{device_name}/{task_name}"
            image_name_template = vals[6] if len(vals) > 6 else "{device_name}_{task_name}_{timestamp}"
            enabled = _bool(vals[7]) if len(vals) > 7 else True
            # If task_def found, it provides everything; if not, these are fallbacks
            if not tdef:
                execution_mode = vals[1]  # Can't do much without a def
                logger.warning("No tasks.json definition for '%s', task may not execute", task_name)
        else:
            # Legacy full-column format — Excel provides everything as before
            match_group = vals[3] if len(vals) > 3 else ""
            match_tags = _parse_tags(vals[4]) if len(vals) > 4 else ()
            if not execution_mode:
                execution_mode = vals[5] if len(vals) > 5 else ""
            if not command_or_url:
                command_or_url = vals[6] if len(vals) > 6 else ""
            if not tdef.get("timeout_seconds"):
                timeout_seconds = _int(vals[10], 60) if len(vals) > 10 else 60
            if not tdef.get("retry_count"):
                retry_count = _int(vals[11], 0) if len(vals) > 11 else 0
            output_dir_template = vals[8] if len(vals) > 8 else "{device_group}/{device_name}/{task_name}"
            image_name_template = vals[9] if len(vals) > 9 else "{device_name}_{task_name}_{timestamp}"
            enabled = _bool(vals[12]) if len(vals) > 12 else True

        # CUSTOM_SCRIPT tasks are always disabled unless explicitly enabled in JSON
        if execution_mode == "CUSTOM_SCRIPT" and not tdef.get("enabled", False):
            enabled = False

        task = Task(
            row_index=i + 2,
            sequence=_int(vals[0]) if len(vals) > 0 else i + 1,
            task_name=task_name,
            task_type=task_type,
            match_group=match_group,
            match_tags=match_tags,
            execution_mode=execution_mode,
            command_or_url=command_or_url,
            actions_json=vals[7] if not is_simplified and len(vals) > 7 else "",
            output_dir_template=output_dir_template,
            image_name_template=image_name_template,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
            enabled=enabled,
            rules_json=rules_json,
        )
        tasks.append(task)

    return tasks


def load_all(
    filepath: str | Path,
    device_sheet: str = "设备信息",
    task_sheet: str = "任务列表",
    tasks_json_path: str | Path | None = None,
) -> tuple[list[Device], list[Task]]:
    return (
        load_devices(filepath, device_sheet),
        load_tasks(filepath, task_sheet, tasks_json_path),
    )
