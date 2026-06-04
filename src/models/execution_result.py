"""ExecutionResult + StepResult — per-task output record.
Execution status and rule status are ALWAYS separate fields.
"""
from dataclasses import dataclass, field
import time

@dataclass
class StepResult:
    step_index: int
    step_name: str
    status: str
    screenshot: str = ""
    details: str = ""
    step_type: str = ""
    variable_extracted: str = ""

@dataclass
class ExecutionResult:
    plan_id: str
    device_name: str
    task_id: str = ""
    client_task_id: str = ""
    device_group: str = ""
    bmc_ip: str = ""
    inband_ip: str = ""
    task_name: str = ""
    task_type: str = ""
    execution_mode: str = ""
    task_sequence: str = ""
    execution_status: str = "EXEC_SUCCESS"
    execution_failure_reason: str = ""
    rule_status: str = "RULE_DISABLED"
    rule_failure_reason: str = ""
    artifact_status: str = "ARTIFACT_PENDING"
    artifact_failure_reason: str = ""
    ready_status: str = "READY_UNKNOWN"
    ready_failure_reason: str = ""
    checkpoint_status: str = "CHECK_DISABLED"
    checkpoint_results: list = field(default_factory=list)
    runtime_context: str = ""
    final_verdict: str = ""
    step_results: list = field(default_factory=list)
    screenshots: tuple = ()
    html_file: str = ""
    txt_file: str = ""
    log_file: str = ""
    output_dir: str = ""
    duration_seconds: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0

    def _checkpoint_summary(self) -> str:
        if not self.checkpoint_results:
            return ""
        counts = {}
        for cr in self.checkpoint_results:
            key = cr.status.replace("CHECK_", "")
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{v}x{k}" for k, v in sorted(counts.items()))

    def to_csv_row(self) -> list:
        return [
            self.plan_id,
            self.task_id,
            self.client_task_id,
            self.device_group,
            self.device_name,
            self.bmc_ip,
            self.inband_ip,
            self.task_sequence,
            self.task_name,
            self.task_type,
            self.execution_mode,
            self.execution_status,
            self.execution_failure_reason,
            self.rule_status,
            self.rule_failure_reason,
            self.artifact_status,
            self.artifact_failure_reason,
            self.checkpoint_status,
            self._checkpoint_summary(),
            self.runtime_context,
            self.final_verdict,
            ";".join(self.screenshots),
            self.html_file,
            self.txt_file,
            self.log_file,
            self.output_dir,
            _fmt_time(self.started_at),
            _fmt_time(self.ended_at),
            str(round(self.duration_seconds, 1)),
            self.ready_status,
            self.ready_failure_reason,
        ]

    @staticmethod
    def csv_header() -> list:
        return [
            "计划ID", "任务ID", "客户端任务ID",
            "设备分类", "设备名称", "带外管理IP", "带内管理IP",
            "任务序号", "任务名称", "任务类型", "执行模式",
            "执行状态", "执行失败原因",
            "规则状态", "规则不符合原因",
            "工件状态", "工件失败原因",
            "检查点状态", "检查点汇总",
            "运行时上下文", "最终结论",
            "截图路径", "HTML路径", "文本路径", "日志路径",
            "输出目录", "开始时间", "结束时间", "耗时秒",
            "就绪状态", "就绪失败原因",
        ]

def _fmt_time(ts: float) -> str:
    if ts <= 0: return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
