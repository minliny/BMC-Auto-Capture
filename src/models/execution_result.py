"""
ExecutionResult + StepResult — per-task output record.
Execution status and rule status are ALWAYS separate fields.
"""


from __future__ import annotations
from dataclasses import dataclass, field
import time


@dataclass
class StepResult:
    step_index: int
    step_name: str
    status: str  # "SUCCESS" | "FAILED"
    screenshot: str = ""
    details: str = ""


@dataclass
class ExecutionResult:
    plan_id: str
    device_name: str
    device_group: str = ""
    bmc_ip: str = ""
    inband_ip: str = ""
    task_name: str = ""
    task_type: str = ""
    execution_mode: str = ""

    execution_status: str = "EXEC_SUCCESS"
    execution_failure_reason: str = ""

    rule_status: str = "RULE_DISABLED"
    rule_failure_reason: str = ""

    step_results: list[StepResult] = field(default_factory=list)
    screenshots: tuple[str, ...] = ()
    html_file: str = ""
    txt_file: str = ""
    log_file: str = ""
    output_dir: str = ""

    duration_seconds: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0

    # --- CSV row export ---

    def to_csv_row(self) -> list[str]:
        return [
            self.device_group,
            self.device_name,
            self.bmc_ip,
            self.inband_ip,
            self.task_name,
            self.task_type,
            self.execution_mode,
            self.execution_status,
            self.execution_failure_reason,
            self.rule_status,
            self.rule_failure_reason,
            ";".join(self.screenshots),
            self.html_file,
            self.txt_file,
            self.log_file,
            self.output_dir,
            _fmt_time(self.started_at),
            _fmt_time(self.ended_at),
            f"{self.duration_seconds:.1f}",
        ]

    @staticmethod
    def csv_header() -> list[str]:
        return [
            "设备分类", "设备名称", "带外管理IP", "带内管理IP",
            "任务名称", "任务类型", "执行模式",
            "执行状态", "执行失败原因",
            "规则状态", "规则不符合原因",
            "截图路径", "HTML路径", "文本路径", "日志路径",
            "输出目录", "开始时间", "结束时间", "耗时秒",
        ]


def _fmt_time(ts: float) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
