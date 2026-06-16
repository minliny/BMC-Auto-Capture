"""Plan-run domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..utils.sensitive import redact_sensitive_text


@dataclass(frozen=True)
class RunConfigSnapshot:
    """Immutable snapshot of latest Excel config at run startup."""

    excel_hash: str
    stored_path: str
    original_filename: str = ""
    source: str = ""
    activated_at: str = ""
    device_count: int = 0
    enabled_device_count: int = 0
    task_count: int = 0
    enabled_task_count: int = 0

    @classmethod
    def from_latest_meta(cls, meta: dict[str, Any]) -> "RunConfigSnapshot":
        return cls(
            excel_hash=meta.get("excelHash", ""),
            stored_path=meta.get("storedPath", ""),
            original_filename=meta.get("originalFilename", ""),
            source=meta.get("source", ""),
            activated_at=meta.get("activatedAt", ""),
            device_count=meta.get("deviceCount", 0),
            enabled_device_count=meta.get("enabledDeviceCount", 0),
            task_count=meta.get("taskCount", 0),
            enabled_task_count=meta.get("enabledTaskCount", 0),
        )


@dataclass
class PlanRunItem:
    plan_id: int | str
    device_name: str
    task_name: str
    task_id: str = ""
    plan_item_id: str = ""
    device_group: str = ""
    task_type: str = ""
    execution_mode: str = ""
    lock_uri: str = ""
    status: str = "PENDING"
    error_message: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    info_events: list[dict[str, Any]] = field(default_factory=list)
    _device: Any = None
    _task: Any = None
    _execution_result: Any = None

    def add_info_event(self, level: str, message: str) -> None:
        from datetime import datetime, timezone

        self.info_events.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        })


@dataclass
class PlanRun:
    """One plan run, identified by the single business plan id."""

    plan_id: int | str
    run_id: str = ""
    excel_hash: str = ""
    status: str = "ACCEPTED"
    runner_mode: str = "fake"
    output_root: str = ""
    items: list[PlanRunItem] = field(default_factory=list)
    updater: str = "downstream-system"
    item_status_url: str = ""
    callback_mode: str = "batch"
    started_at: float = 0.0
    finished_at: float = 0.0
    config_snapshot: RunConfigSnapshot | None = None

    @property
    def summary(self) -> dict[str, Any]:
        failed_items = [
            {
                "taskId": item.task_id,
                "planItemId": item.plan_item_id,
                "deviceGroup": item.device_group,
                "deviceName": item.device_name,
                "taskName": item.task_name,
                "errorMessage": (
                    redact_sensitive_text(item.error_message or "")
                    if item.error_message else None
                ),
            }
            for item in self.items if item.status == "FAILED"
        ]
        return {
            "total": len(self.items),
            "success": sum(1 for item in self.items if item.status == "SUCCESS"),
            "failed": sum(1 for item in self.items if item.status == "FAILED"),
            "in_progress": sum(1 for item in self.items if item.status == "IN_PROGRESS"),
            "pending": sum(1 for item in self.items if item.status == "PENDING"),
            "failureSummary": failed_items,
            "outputRoot": self.output_root,
        }

    @property
    def is_external(self) -> bool:
        return bool(self.excel_hash)
