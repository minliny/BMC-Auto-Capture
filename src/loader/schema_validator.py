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

    def check_results(self):
        from ..checks import check_results_from_validation_report

        return check_results_from_validation_report(self, source_prefix="loader.validation")

    def check_results_as_dicts(self) -> list[dict]:
        return [c.to_dict() for c in self.check_results()]


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
    task_ids: set[str] = set()
    for t in tasks:
        task_id = getattr(t, "task_id", "") or ""
        if t.enabled and not task_id:
            report.messages.append(ValidationMessage(
                "WARNING", "task", t.row_index, "任务ID",
                "启用任务缺少任务ID，当前仅按任务名称兼容匹配；后续版本会要求任务ID",
            ))
        if task_id:
            if task_id in task_ids:
                report.messages.append(ValidationMessage(
                    "ERROR", "task", t.row_index, "任务ID", f"任务ID重复: {task_id}",
                ))
            task_ids.add(task_id)

        if not t.task_name:
            report.messages.append(ValidationMessage("ERROR", "task", t.row_index, "任务名称", "任务名称不能为空"))

        if t.task_name in task_names:
            report.messages.append(ValidationMessage(
                "WARNING", "task", t.row_index, "任务名称",
                f"任务名称重复: {t.task_name}；任务匹配以任务ID为准",
            ))
        task_names.add(t.task_name)

        if t.task_type.upper() not in ("BMC", "SSH", "TELNET"):
            report.messages.append(ValidationMessage("ERROR", "task", t.row_index, "任务类型", f"不支持的任务类型: '{t.task_type}'，应填 BMC/SSH/TELNET"))

        if t.execution_mode not in ("BMC_URL", "BMC_ACTIONS", "SSH_CMD", "TELNET_CMD", ""):
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行模式", f"未知执行模式: '{t.execution_mode}'"))

        if t.execution_mode in ("BMC_URL",) and not t.command_or_url:
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行命令", "BMC_URL 任务未填写 URL"))

        if t.execution_mode in ("SSH_CMD", "TELNET_CMD") and not t.command_or_url:
            report.messages.append(ValidationMessage("WARNING", "task", t.row_index, "执行命令", f"{t.execution_mode} 任务未填写命令"))

    # --- Cross-validation: device_group vs task match_group consistency ---
    device_groups_lower = {d.device_group.lower() for d in devices if d.device_group}

    # Expand multi-group tasks (e.g. "L1/L2" → {"l1", "l2"})
    def _expand_groups(group: str) -> set[str]:
        return {g.strip().lower() for g in group.split("/") if g.strip()}

    for t in tasks:
        if not t.match_group:
            continue
        task_groups = _expand_groups(t.match_group)
        if not task_groups & device_groups_lower:
            report.messages.append(ValidationMessage(
                "WARNING", "cross", t.row_index, "设备分组",
                f"任务 '{t.task_name}' 的设备分组 '{t.match_group}' 在设备信息表中不存在"
            ))

    # Warn if any device_group doesn't appear in any task's match_group
    all_task_groups: set[str] = set()
    for t in tasks:
        if t.match_group:
            all_task_groups |= _expand_groups(t.match_group)

    for d in devices:
        if d.device_group and d.enabled and d.device_group.lower() not in all_task_groups:
            report.messages.append(ValidationMessage(
                "WARNING", "cross", d.row_index, "设备分组",
                f"设备 '{d.device_name}' 的设备分组 '{d.device_group}' 没有任务匹配"
            ))

    return report
