"""
Excel V2 reader — parses 设备信息 and 任务列表 sheets into Device and Task lists.
"""


from __future__ import annotations
from pathlib import Path
from typing import Any
import openpyxl

from ..models.device import Device
from ..models.task import Task


def _str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _bool(val: Any) -> bool:
    """Interpret common truthy/falsy Excel values."""
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


def load_tasks(filepath: str | Path, sheet_name: str = "任务列表") -> list[Task]:
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
        match_tags = _parse_tags(vals[4]) if len(vals) > 4 else ()

        task = Task(
            row_index=i + 2,
            sequence=_int(vals[0]) if len(vals) > 0 else i + 1,
            task_name=vals[1] if len(vals) > 1 else "",
            task_type=vals[2] if len(vals) > 2 else "",
            match_group=vals[3] if len(vals) > 3 else "",
            match_tags=match_tags,
            execution_mode=vals[5] if len(vals) > 5 else "",
            command_or_url=vals[6] if len(vals) > 6 else "",
            actions_json=vals[7] if len(vals) > 7 else "",
            output_dir_template=vals[8] if len(vals) > 8 else "{device_name}/{task_name}",
            image_name_template=vals[9] if len(vals) > 9 else "{device_name}_{task_name}_{step}_{timestamp}",
            timeout_seconds=_int(vals[10], 60) if len(vals) > 10 else 60,
            retry_count=_int(vals[11], 0) if len(vals) > 11 else 0,
            enabled=_bool(vals[12]) if len(vals) > 12 else True,
            rules_json=vals[13] if len(vals) > 13 else "",
        )
        tasks.append(task)

    return tasks


def load_all(
    filepath: str | Path,
    device_sheet: str = "设备信息",
    task_sheet: str = "任务列表",
) -> tuple[list[Device], list[Task]]:
    return (
        load_devices(filepath, device_sheet),
        load_tasks(filepath, task_sheet),
    )
