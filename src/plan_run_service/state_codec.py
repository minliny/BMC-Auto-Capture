"""Serialization for persisted plan run state."""

from __future__ import annotations

from typing import Any

from ..utils.sensitive import redact_sensitive_text
from .models import PlanRun, PlanRunItem


class PlanRunStateCodec:
    """Convert PlanRun objects to and from persisted JSON-compatible state."""

    def run_to_state(self, run: PlanRun) -> dict[str, Any]:
        return {
            "version": 1,
            "planId": str(run.plan_id),
            "runId": run.run_id,
            "excelHash": run.excel_hash,
            "status": run.status,
            "runnerMode": run.runner_mode,
            "outputRoot": run.output_root,
            "updater": run.updater,
            "callbackMode": run.callback_mode,
            "startedAt": run.started_at,
            "finishedAt": run.finished_at,
            "items": [
                {
                    "planId": str(item.plan_id),
                    "taskId": item.task_id,
                    "planItemId": item.plan_item_id,
                    "deviceName": item.device_name,
                    "taskName": item.task_name,
                    "deviceGroup": item.device_group,
                    "taskType": item.task_type,
                    "executionMode": item.execution_mode,
                    "status": item.status,
                    "errorMessage": (
                        redact_sensitive_text(item.error_message or "")
                        if item.error_message else None
                    ),
                    "startedAt": item.started_at,
                    "finishedAt": item.finished_at,
                    "infoEvents": item.info_events,
                }
                for item in run.items
            ],
        }

    def state_to_run(self, data: dict[str, Any]) -> PlanRun | None:
        plan_id = data.get("planId", "")
        run_id = data.get("runId", "")
        if not plan_id or not run_id:
            return None

        status = str(data.get("status", ""))
        if status in ("ACCEPTED", "RUNNING"):
            status = "INTERRUPTED"

        items: list[PlanRunItem] = []
        for raw in data.get("items", []) or []:
            if not isinstance(raw, dict):
                continue
            item_status = str(raw.get("status", "PENDING"))
            if item_status in ("PENDING", "IN_PROGRESS", "RUNNING"):
                item_status = "FAILED" if status == "INTERRUPTED" else item_status
            items.append(PlanRunItem(
                plan_id=plan_id,
                device_name=str(raw.get("deviceName", "")),
                task_name=str(raw.get("taskName", "")),
                task_id=str(raw.get("taskId", "")),
                plan_item_id=str(raw.get("planItemId", "")),
                device_group=str(raw.get("deviceGroup", "")),
                task_type=str(raw.get("taskType", "")),
                execution_mode=str(raw.get("executionMode", "")),
                status=item_status,
                error_message=raw.get("errorMessage"),
                started_at=float(raw.get("startedAt", 0.0) or 0.0),
                finished_at=float(raw.get("finishedAt", 0.0) or 0.0),
                info_events=list(raw.get("infoEvents", []) or []),
            ))

        return PlanRun(
            plan_id=plan_id,
            run_id=str(run_id),
            excel_hash=str(data.get("excelHash", "")),
            status=status or "UNKNOWN",
            runner_mode=str(data.get("runnerMode", "fake")),
            output_root=str(data.get("outputRoot", "")),
            items=items,
            updater=str(data.get("updater", "downstream-system")),
            item_status_url="",
            callback_mode=str(data.get("callbackMode", "batch")),
            started_at=float(data.get("startedAt", 0.0) or 0.0),
            finished_at=float(data.get("finishedAt", 0.0) or 0.0),
        )
