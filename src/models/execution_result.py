"""ExecutionResult + StepResult — per-task output record.
Execution status and rule status are ALWAYS separate fields.
"""
from dataclasses import dataclass, field
import time
from typing import Any

from ..checks.models import CheckResult

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
    plan_item_id: str = ""
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
    raw_screenshots: tuple = ()
    html_file: str = ""
    txt_file: str = ""
    log_file: str = ""
    output_dir: str = ""
    duration_seconds: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    # New endpoint-aware + timing fields
    endpoint_key: str = ""
    endpoint_type: str = ""
    resource_wait_seconds: float = 0.0
    executor_duration_seconds: float = 0.0
    retry_count: int = 0
    attempt_records: list = field(default_factory=list)  # list[AttemptRecord] — AUDIT-007
    attempt_count: int = 1
    max_attempts: int = 1
    final_attempt_index: int = 1
    retry_reasons: list[str] = field(default_factory=list)
    unknown_status: bool = False
    raw_execution_status: str = ""
    check_results: list = field(default_factory=list)

    def __post_init__(self):
        if not self.plan_item_id and self.plan_id and self.device_name and self.task_id:
            self.plan_item_id = f"{self.plan_id}:{self.device_name}:{self.task_id}"

    def _checkpoint_summary(self) -> str:
        if not self.checkpoint_results:
            return ""
        counts = {}
        for cr in self.checkpoint_results:
            key = cr.status.replace("CHECK_", "")
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{v}x{k}" for k, v in sorted(counts.items()))

    def add_check_result(self, check_result: CheckResult | dict[str, Any]) -> None:
        if isinstance(check_result, dict):
            check_result = CheckResult.from_dict(check_result)
        self.check_results.append(check_result)

    def check_result_summary(self) -> str:
        if not self.check_results:
            return ""
        counts: dict[str, int] = {}
        for cr in self._iter_check_results():
            key = f"{cr.stage}:{cr.status}"
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{v}x{k}" for k, v in sorted(counts.items()))

    def check_failure_summary(self, limit: int = 5) -> str:
        details: list[str] = []
        for cr in self._iter_check_results():
            if cr.status not in ("FAIL", "ERROR", "WARN"):
                continue
            message = cr.message or cr.actual or cr.target
            structured = _format_check_failure_details(cr.details)
            if structured and structured not in message:
                message = f"{message} ({structured})" if message else structured
            details.append(f"{cr.stage}/{cr.check_id}/{cr.status}: {message}")
            if len(details) >= limit:
                break
        return " | ".join(details)

    def check_results_as_dicts(self) -> list[dict[str, Any]]:
        return [cr.to_dict() for cr in self._iter_check_results()]

    def _iter_check_results(self):
        for cr in self.check_results or ():
            if isinstance(cr, CheckResult):
                yield cr
            elif isinstance(cr, dict):
                yield CheckResult.from_dict(cr)

    def to_csv_row(self) -> list:
        return [
            self.plan_id,
            self.task_id,
            self.plan_item_id,
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
            # New endpoint-aware + timing fields
            self.endpoint_key,
            self.endpoint_type,
            _fmt_time(self.started_at),
            _fmt_time(self.ended_at),
            str(round(self.duration_seconds, 1)),
            str(round(self.resource_wait_seconds, 1)),
            str(round(self.executor_duration_seconds, 1)),
            str(self.attempt_count),
            str(self.max_attempts),
            str(self.final_attempt_index),
            " | ".join(self.retry_reasons),
            str(self.unknown_status).lower(),
            self.raw_execution_status,
            self.check_result_summary(),
            self.check_failure_summary(),
        ]

    @staticmethod
    def csv_header() -> list:
        return [
            "计划ID", "任务ID", "执行项ID", "客户端任务ID",
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
            # New
            "endpoint_key", "endpoint_type",
            "started_at", "ended_at",
            "duration_seconds", "resource_wait_seconds", "executor_duration_seconds",
            "attempt_count", "max_attempts", "final_attempt_index", "retry_reasons",
            "unknown_status", "raw_execution_status",
            "检查结果汇总", "检查失败明细",
        ]

def _fmt_time(ts: float) -> str:
    if ts <= 0: return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _format_check_failure_details(details: dict[str, Any]) -> str:
    if not isinstance(details, dict):
        return ""
    hits: list[dict[str, Any]] = []
    direct = _extract_structured_hit(details)
    if direct:
        hits.append(direct)
    for failure in details.get("failures", []):
        if not isinstance(failure, dict):
            continue
        failure_details = failure.get("details", {})
        if not isinstance(failure_details, dict):
            continue
        matches = failure_details.get("matches", [])
        has_matches = isinstance(matches, list) and bool(matches)
        if has_matches:
            hits.extend(match for match in matches if isinstance(match, dict))
        else:
            nested = _extract_structured_hit(failure_details)
            if nested:
                hits.append(nested)
    formatted = [_format_structured_hit(hit) for hit in hits[:3]]
    return "; ".join(part for part in formatted if part)


def _extract_structured_hit(details: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    keys = ("interface", "field", "value", "raw_line")
    return {key: details[key] for key in keys if details.get(key) not in ("", None)}


def _format_structured_hit(hit: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("interface", "field", "value", "raw_line"):
        if key not in hit:
            continue
        value = hit[key]
        if key in ("value", "raw_line"):
            parts.append(f"{key}={value!r}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)
