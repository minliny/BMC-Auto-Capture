"""Plan-run report generation adapters."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..models.execution_result import ExecutionResult
from ..out.result_writer import ResultWriter
from ..utils.sensitive import redact_sensitive_text

logger = logging.getLogger("bmc_auto_capture.plan_run.reports")


class PlanRunResultReporter:
    """Converts plan-run items to execution results and writes report artifacts."""

    def __init__(self, result_writer: Any = None):
        self._result_writer = result_writer or ResultWriter()

    def write(self, run: Any) -> None:
        if not getattr(run, "output_root", ""):
            return
        try:
            output_root = Path(run.output_root)
            output_root.mkdir(parents=True, exist_ok=True)
            results = [self.execution_result_for_item(run, item) for item in run.items]
            if not results:
                return

            self._result_writer.write(
                results,
                str(output_root),
                execution_started_at=run.started_at,
                execution_id=str(run.run_id or ""),
                emit_terminal_summary=False,
            )
        except Exception as exc:
            logger.warning("PlanRun report generation failed: %s", redact_sensitive_text(str(exc)))

    @staticmethod
    def execution_result_for_item(run: Any, item: Any) -> ExecutionResult:
        result = item._execution_result
        if result is None:
            status = "EXEC_SUCCESS" if item.status == "SUCCESS" else "EXEC_FAILED"
            if item.status in ("PENDING", "IN_PROGRESS"):
                status = f"EXEC_{item.status}"
            started_at = item.started_at or run.started_at
            ended_at = item.finished_at or run.finished_at or started_at
            device = item._device
            task = item._task
            return ExecutionResult(
                plan_id=str(run.plan_id),
                device_name=item.device_name,
                task_id=item.task_id,
                plan_item_id=item.plan_item_id,
                device_group=item.device_group,
                bmc_ip=getattr(device, "bmc_ip", "") if device is not None else "",
                inband_ip=getattr(device, "inband_ip", "") if device is not None else "",
                task_name=item.task_name,
                task_type=item.task_type,
                execution_mode=item.execution_mode,
                task_sequence=str(
                    getattr(task, "sequence_str", "")
                    or getattr(task, "sequence", "")
                    or ""
                ),
                execution_status=status,
                execution_failure_reason=item.error_message or "",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=max(0.0, ended_at - started_at),
                output_dir=run.output_root,
            )

        result.plan_id = str(run.plan_id)
        result.task_id = result.task_id or item.task_id
        result.plan_item_id = result.plan_item_id or item.plan_item_id
        result.device_name = result.device_name or item.device_name
        result.device_group = result.device_group or item.device_group
        result.task_name = result.task_name or item.task_name
        result.task_type = result.task_type or item.task_type
        result.execution_mode = result.execution_mode or item.execution_mode
        task = item._task
        if not getattr(result, "task_sequence", ""):
            result.task_sequence = str(
                getattr(task, "sequence_str", "")
                or getattr(task, "sequence", "")
                or ""
            )
        if not getattr(result, "started_at", 0):
            result.started_at = item.started_at
        if not getattr(result, "ended_at", 0):
            result.ended_at = item.finished_at
        if not getattr(result, "duration_seconds", 0) and result.started_at and result.ended_at:
            result.duration_seconds = max(0.0, result.ended_at - result.started_at)
        if not getattr(result, "output_dir", ""):
            result.output_dir = run.output_root
        return result
