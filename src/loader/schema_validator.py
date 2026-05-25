"""
Schema validator — structural and semantic validation of loaded devices/tasks.
Returns a ValidationReport with errors and warnings.
"""


from __future__ import annotations
from dataclasses import dataclass, field

from ..models.device import Device
from ..models.task import Task


@dataclass
class ValidationMessage:
    level: str  # "ERROR" | "WARNING"
    source: str  # "device" | "task" | "cross"
    row: int
    field: str
    message: str


@dataclass
class ValidationReport:
    messages: list[ValidationMessage] = field(default_factory=list)
    device_count: int = 0
    device_enabled_count: int = 0
    task_count: int = 0
    task_enabled_count: int = 0

    @property
    def errors(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.level == "ERROR"]

    @property
    def warnings(self) -> list[ValidationMessage]:
        return [m for m in self.messages if m.level == "WARNING"]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def validate(devices: list[Device], tasks: list[Task]) -> ValidationReport:
    report = ValidationReport(
        device_count=len(devices),
        device_enabled_count=sum(1 for d in devices if d.enabled),
        task_count=len(tasks),
        task_enabled_count=sum(1 for t in tasks if t.enabled),
    )

    # --- Device validation ---
    device_names: set[str] = set()
    for d in devices:
        if not d.device_name:
            report.messages.append(ValidationMessage("ERROR", "device", d.row_index, "设备名称", "设备名称不能为空"))

        if d.device_name in device_names:
            report.messages.append(ValidationMessage("WARNING", "device", d.row_index, "设备名称", f"设备名称重复: {d.device_name}"))
        device_names.add(d.device_name)

        if d.enabled:
            if not d.bmc_ip:
                report.messages.append(ValidationMessage("WARNING", "device", d.row_index, "带外管理IP", "启用设备的BMC IP为空"))
            if not d.inband_ip:
                report.messages.append(ValidationMessage("WARNING", "device", d.row_index, "带内管理IP", "启用设备的带内IP为空，SSH任务将跳过"))

    # --- Task validation ---
    task_names: set[str] = set()
    for t in tasks:
        if not t.task_name:
            report.messages.append(ValidationMessage("ERROR", "task", t.row_index, "任务名称", "任务名称不能为空"))

        if t.task_name in task_names:
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "任务名称", f"任务名称重复: {t.task_name}"))
        task_names.add(t.task_name)

        if t.task_type.upper() not in ("BMC", "SSH", "TELNET"):
            report.messages.append(ValidationMessage("ERROR", "task", t.row_index, "任务类型", f"不支持的任务类型: '{t.task_type}'，应填 BMC/SSH/TELNET"))

        if t.execution_mode not in ("BMC_URL", "BMC_ACTIONS", "SSH_CMD", "TELNET_CMD", ""):
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行模式", f"未知执行模式: '{t.execution_mode}'"))

        if t.execution_mode in ("BMC_URL",) and not t.command_or_url:
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行命令", "BMC_URL 任务未填写 URL"))

        if t.execution_mode in ("SSH_CMD", "TELNET_CMD") and not t.command_or_url:
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行命令", f"{t.execution_mode} 任务未填写命令"))

    # --- Cross-validation: check that enabled tasks have matching enabled devices ---
    enabled_groups = {d.device_group for d in devices if d.enabled}
    enabled_tags: set[str] = set()
    for d in devices:
        if d.enabled:
            enabled_tags.update(d.tags)

    for t in tasks:
        if not t.enabled:
            continue
        if t.match_group and t.match_group not in enabled_groups:
            report.messages.append(ValidationMessage(
                "WARNING", "cross", t.row_index, "设备分组",
                f"任务 '{t.task_name}' 匹配的设备分组 '{t.match_group}' 中没有启用的设备"
            ))
        if t.match_tags:
            unmatched = [tag for tag in t.match_tags if tag not in enabled_tags]
            if unmatched:
                report.messages.append(ValidationMessage(
                    "WARNING", "cross", t.row_index, "标签",
                    f"任务 '{t.task_name}' 的标签 {unmatched} 在任何启用设备上都不存在"
                ))

    return report
