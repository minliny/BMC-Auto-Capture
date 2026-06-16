"""
公共工具函数：模板解析、设备信息渲染、超时处理等
"""

from __future__ import annotations
import logging
import time
import re
from typing import Optional

logger = logging.getLogger("bmc_auto_capture.utils")


def resolve_template(tmpl: str, device, task, extra: Optional[dict] = None) -> str:
    """统一模板变量解析函数。

    支持中英文字段名，同时支持 Excel 表头名和旧版英文变量名。
    未识别的变量原样保留，可用于检测残留花括号。

    P0-2: 不允许密码变量进入路径/文件名/日志/CSV。
    调用 resolve_template 前必须用 check_forbidden_template_vars() 检查。

    Args:
        tmpl: 模板字符串，可能包含 {变量名}
        device: Device 对象，包含 device_name, device_group, bmc_ip 等
        task: Task 对象，包含 task_name, task_sequence 等
        extra: 可选的额外变量字典

    Returns:
        渲染后的字符串

    Examples:
        resolve_template("{任务名称}/{设备分类}", device, task)
        resolve_template("{device_name}/{device_group}", device, task)
    """
    seq = task.sequence_str or str(task.sequence)
    ts = time.strftime("%Y%m%d_%H%M%S")

    result = tmpl
    # 中文变量 (Excel 表头名)
    result = result.replace("{任务ID}", getattr(task, "task_id", "") or task.task_name)
    result = result.replace("{任务序号}", seq)
    result = result.replace("{任务名称}", task.task_name)
    result = result.replace("{任务类型}", task.task_type)
    result = result.replace("{设备分类}", device.device_group)
    result = result.replace("{设备名称}", device.device_name)
    result = result.replace("{带外管理IP}", device.bmc_ip)
    result = result.replace("{带外管理用户名}", device.bmc_username)
    # P0-2: 密码变量替换为 REDACTED，防止泄露到路径/文件名
    result = result.replace("{带外管理密码}", "REDACTED")
    result = result.replace("{带内管理IP}", device.inband_ip)
    result = result.replace("{带内管理用户名}", device.inband_username)
    result = result.replace("{带内管理密码}", "REDACTED")
    result = result.replace("{设备标签}", getattr(device, "tags", ""))

    # 英文变量 (旧版默认模板兼容)
    result = result.replace("{task_id}", getattr(task, "task_id", "") or task.task_name)
    result = result.replace("{task_sequence}", seq)
    result = result.replace("{task_name}", task.task_name)
    result = result.replace("{task_type}", task.task_type)
    result = result.replace("{device_group}", device.device_group)
    result = result.replace("{device_name}", device.device_name)
    result = result.replace("{device_ip}", device.bmc_ip)
    result = result.replace("{bmc_ip}", device.bmc_ip)
    result = result.replace("{inband_ip}", device.inband_ip)
    result = result.replace("{ib_ip}", device.inband_ip)
    result = result.replace("{step}", "final")
    result = result.replace("{timestamp}", ts)
    result = result.replace("{tags}", getattr(device, "tags", ""))

    # 英文别名 (双语表头兼容)
    result = result.replace("{TaskID}", getattr(task, "task_id", "") or task.task_name)
    result = result.replace("{TaskName}", task.task_name)
    result = result.replace("{TaskType}", task.task_type)
    result = result.replace("{TaskSequence}", seq)
    result = result.replace("{DeviceGroup}", device.device_group)
    result = result.replace("{DeviceName}", device.device_name)
    result = result.replace("{OOB_IP}", device.bmc_ip)
    result = result.replace("{OOB_Username}", device.bmc_username)
    result = result.replace("{OOB_Password}", "REDACTED")
    result = result.replace("{IB_IP}", device.inband_ip)
    result = result.replace("{IB_Username}", device.inband_username)
    result = result.replace("{IB_Password}", "REDACTED")
    result = result.replace("{OutputDir}", "")
    result = result.replace("{FileNamePattern}", "")

    # 智能 IP：BMC → 带外IP，SSH/TELNET → 带内IP
    task_ip = device.bmc_ip if task.task_type.upper() in ("BMC",) else device.inband_ip
    result = result.replace("{TaskIP}", task_ip)

    # 额外变量
    if extra:
        for key, value in extra.items():
            result = result.replace(f"{{{key}}}", str(value))

    # 检测未替换的变量
    unreplaced = re.findall(r'\{[^}]+\}', result)
    if unreplaced:
        logger.warning(f"模板残留未替换变量: {unreplaced} in '{tmpl}'")

    return result


def check_unreplaced_vars(tmpl: str) -> list[str]:
    """检测模板中是否残留未替换的花括号变量。

    Args:
        tmpl: 模板字符串

    Returns:
        未替换的变量列表，如 ["{task_name}", "{device_group}"]
    """
    return re.findall(r'\{[^}]+\}', tmpl)
