"""
Job — one device × one task × one attempt execution unit.
Uses task_snapshot (not command).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELED = "CANCELED"
    LOST = "LOST"
    SKIPPED = "SKIPPED"


@dataclass
class StepResult:
    step_index: int = 0
    step_name: str = ""
    status: str = ""
    step_type: str = ""
    duration_ms: int = 0
    details: str = ""
    screenshot_artifact_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "step_name": self.step_name,
            "status": self.status,
            "step_type": self.step_type,
            "duration_ms": self.duration_ms,
            "details": self.details,
            "screenshot_artifact_id": self.screenshot_artifact_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepResult":
        return cls(
            step_index=int(d.get("step_index", 0)),
            step_name=d.get("step_name", ""),
            status=d.get("status", ""),
            step_type=d.get("step_type", ""),
            duration_ms=int(d.get("duration_ms", 0)),
            details=d.get("details", ""),
            screenshot_artifact_id=d.get("screenshot_artifact_id", ""),
        )


@dataclass
class Job:
    job_id: str
    run_id: str = ""
    device_id: str = ""
    task_id: str = ""
    attempt: int = 1
    max_attempts: int = 3
    status: JobStatus = JobStatus.QUEUED
    resource_lock_uri: str = ""
    executor_id: str = ""
    task_snapshot: dict[str, Any] = field(default_factory=dict)
    device_snapshot: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 60
    created_at: str = ""
    queued_at: str = ""
    dispatched_at: str = ""
    accepted_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    step_results: list[StepResult] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "device_id": self.device_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "status": self.status.value,
            "resource_lock_uri": self.resource_lock_uri,
            "executor_id": self.executor_id,
            "task_snapshot": dict(self.task_snapshot),
            "device_snapshot": dict(self.device_snapshot),
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "queued_at": self.queued_at,
            "dispatched_at": self.dispatched_at,
            "accepted_at": self.accepted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "step_results": [s.to_dict() for s in self.step_results],
            "error": dict(self.error) if self.error else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        steps = [StepResult.from_dict(s) for s in d.get("step_results", [])]
        return cls(
            job_id=d["job_id"],
            run_id=d.get("run_id", ""),
            device_id=d.get("device_id", ""),
            task_id=d.get("task_id", ""),
            attempt=int(d.get("attempt", 1)),
            max_attempts=int(d.get("max_attempts", 3)),
            status=JobStatus(d.get("status", "QUEUED")),
            resource_lock_uri=d.get("resource_lock_uri", ""),
            executor_id=d.get("executor_id", ""),
            task_snapshot=dict(d.get("task_snapshot", {})),
            device_snapshot=dict(d.get("device_snapshot", {})),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            created_at=d.get("created_at", ""),
            queued_at=d.get("queued_at", ""),
            dispatched_at=d.get("dispatched_at", ""),
            accepted_at=d.get("accepted_at", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            duration_ms=int(d.get("duration_ms", 0)),
            step_results=steps,
            error=dict(d["error"]) if d.get("error") else None,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.TIMEOUT,
            JobStatus.CANCELED,
            JobStatus.LOST,
            JobStatus.SKIPPED,
        )

    @property
    def is_retryable(self) -> bool:
        return self.status in (JobStatus.FAILED, JobStatus.TIMEOUT) and self.attempt < self.max_attempts
