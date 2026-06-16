"""Public response projection for plan-run query APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..utils.sensitive import redact_sensitive_text


def format_timestamp(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class PlanRunQueryProjector:
    """Projects internal plan-run objects into public API dictionaries."""

    def item(self, item: Any) -> dict[str, Any]:
        return {
            "deviceGroup": item.device_group,
            "deviceName": item.device_name,
            "taskName": item.task_name,
            "status": item.status,
            "errorMessage": redact_sensitive_text(item.error_message or "") if item.error_message else None,
            "startedAt": format_timestamp(item.started_at) if item.started_at else None,
            "finishedAt": format_timestamp(item.finished_at) if item.finished_at else None,
            "infoEvents": item.info_events,
        }

    def plan(self, run: Any, include_items: bool = False) -> dict[str, Any]:
        result = {
            "planId": run.plan_id,
            "status": run.status,
            "summary": run.summary,
            "excelHash": run.excel_hash,
            "outputRoot": run.output_root,
            "startedAt": format_timestamp(run.started_at),
            "finishedAt": format_timestamp(run.finished_at),
            "errorMessage": (
                redact_sensitive_text(run.error_message or "")
                if getattr(run, "error_message", "")
                else None
            ),
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
