"""
Pydantic schemas for executor API request/response validation.
"""

from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class CallbackInfo(BaseModel):
    status_url: str = ""
    artifact_url: str = ""
    auth_token: str = ""


class ResourceLockInfo(BaseModel):
    lock_uri: str = ""
    lock_exclusive: bool = True


class DeviceSnapshotPayload(BaseModel):
    device_id: str = ""
    device_name: str = ""
    device_group: str = ""
    oob_ip: str = ""
    inband_ip: str = ""
    oob_username: str = ""
    oob_password_ref: str = ""
    inband_username: str = ""
    inband_password_ref: str = ""


class TaskSnapshotPayload(BaseModel):
    task_id: str = ""
    task_no: str = ""
    task_name: str = ""
    task_type: str = ""
    execution_mode: str = ""
    url: str = ""
    command_or_url: str = ""
    actions_json: str = ""
    timeout_seconds: int = 60
    retry_count: int = 0
    output_dir_template: str = "{device_name}/{task_name}"
    image_name_template: str = "{device_name}_{task_name}_{step}_{timestamp}"
    full_screenshot: bool = False
    screenshot_mode: str = "auto"


class JobPayload(BaseModel):
    job_id: str = ""
    run_id: str = ""
    attempt: int = 1
    resource_lock: ResourceLockInfo = Field(default_factory=ResourceLockInfo)
    device_snapshot: DeviceSnapshotPayload = Field(default_factory=DeviceSnapshotPayload)
    task_snapshot: TaskSnapshotPayload = Field(default_factory=TaskSnapshotPayload)


class JobDispatchRequest(BaseModel):
    command_id: str
    command_type: str = "ASSIGN_JOB"
    external_task_id: str
    callback: CallbackInfo = Field(default_factory=CallbackInfo)
    job: JobPayload = Field(default_factory=JobPayload)


class JobAcceptResponse(BaseModel):
    accepted: bool
    external_task_id: str = ""
    job_id: str = ""
    status: str = ""
    message: str = ""
    duplicate: bool = False


class JobStatusResponse(BaseModel):
    job_id: str
    external_task_id: str = ""
    command_id: str = ""
    status: str = ""
    received_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    last_callback_error: str = ""
    error: Optional[dict[str, Any]] = None
    result_summary: dict[str, Any] = Field(default_factory=dict)


class ExecutorStatusResponse(BaseModel):
    executor_id: str
    status: str = "ONLINE"
    version: str = "0.2.4"
    job_counts: dict[str, int] = Field(default_factory=dict)
    total_jobs: int = 0
    uptime_seconds: float = 0.0
