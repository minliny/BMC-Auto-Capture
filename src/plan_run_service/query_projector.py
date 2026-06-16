"""Public response projection for plan-run query APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..checks import CheckResult
from ..utils.sensitive import redact_sensitive_text


def format_timestamp(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class PlanRunQueryProjector:
    """Projects internal plan-run objects into public API dictionaries."""

    def item(self, item: Any) -> dict[str, Any]:
        result = {
            "taskId": item.task_id,
            "planItemId": item.plan_item_id,
            "deviceGroup": item.device_group,
            "deviceName": item.device_name,
            "taskName": item.task_name,
            "status": item.status,
            "errorMessage": redact_sensitive_text(item.error_message or "") if item.error_message else None,
            "startedAt": format_timestamp(item.started_at) if item.started_at else None,
            "finishedAt": format_timestamp(item.finished_at) if item.finished_at else None,
            "infoEvents": item.info_events,
        }
        execution_result = getattr(item, "_execution_result", None)
        if execution_result is not None:
            result.update({
                "executionStatus": getattr(execution_result, "execution_status", ""),
                "ruleStatus": getattr(execution_result, "rule_status", ""),
                "artifactStatus": getattr(execution_result, "artifact_status", ""),
                "readyStatus": getattr(execution_result, "ready_status", ""),
                "checkpointStatus": getattr(execution_result, "checkpoint_status", ""),
                "finalVerdict": getattr(execution_result, "final_verdict", ""),
                "checkResults": [
                    self._public_check_result(cr)
                    for cr in (getattr(execution_result, "check_results", None) or [])
                ],
            })
        return result

    def plan(self, run: Any, include_items: bool = False) -> dict[str, Any]:
        result = {
            "planId": run.plan_id,
            "status": run.status,
            "summary": run.summary,
            "excelHash": run.excel_hash,
            "outputRoot": run.output_root,
            "startedAt": format_timestamp(run.started_at),
            "finishedAt": format_timestamp(run.finished_at),
            "errorMessage": None,
            "infoEvents": [
                {
                    "timestamp": format_timestamp(run.started_at),
                    "level": "INFO",
                    "message": f"Plan started: planId={run.plan_id}",
                } if run.started_at else None,
                {
                    "timestamp": format_timestamp(run.finished_at),
                    "level": "INFO",
                    "message": f"Plan finished: status={run.status}",
                } if run.finished_at else None,
            ],
        }
        result["infoEvents"] = [ev for ev in result["infoEvents"] if ev]
        if include_items:
            result["items"] = [self.item(item) for item in run.items]
        return result

    @staticmethod
    def _public_check_result(check_result: Any) -> dict[str, Any]:
        if isinstance(check_result, dict):
            check_result = CheckResult.from_dict(check_result)
        details = {}
        for key, value in (getattr(check_result, "details", {}) or {}).items():
            redacted_pair = redact_sensitive_text(f"{key}={value}")
            prefix = f"{key}="
            details[str(key)] = (
                redacted_pair[len(prefix):]
                if redacted_pair.startswith(prefix)
                else redacted_pair
            )
        return {
            "stage": getattr(check_result, "stage", ""),
            "checkId": getattr(check_result, "check_id", ""),
            "status": getattr(check_result, "status", ""),
            "severity": getattr(check_result, "severity", ""),
            "message": redact_sensitive_text(str(getattr(check_result, "message", "") or "")),
            "details": details,
        }
