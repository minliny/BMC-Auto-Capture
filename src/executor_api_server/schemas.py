"""
Pydantic schemas for executor API request/response validation.
"""

from __future__ import annotations
from typing import Any, Optional, Union
from pydantic import BaseModel, ConfigDict, Field

from src._version import APP_VERSION


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
    model_config = ConfigDict(extra="allow")

    plan_id: Union[str, int] = ""
    plan_item_id: str = ""
    task_id: str = ""
    task_no: str = ""
    task_name: str = ""
    sequence: int = 0
    sequence_str: str = ""
    task_type: str = ""
    execution_mode: str = ""
    match_group: str = ""
    url: str = ""
    command_or_url: str = ""
    raw_command_or_url: str = ""
    ssh_cmd: str = ""
    command: str = ""
    actions_json: str = ""
    rules_json: str = ""
    rules: list[dict[str, Any]] = Field(default_factory=list)
    result_rules: list[dict[str, Any]] = Field(default_factory=list)
    ssh_rules: list[dict[str, Any]] = Field(default_factory=list)
    task_def: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 60
    retry_count: int = 0
    output_dir_template: str = "{device_name}/{task_name}"
    image_name_template: str = "{device_name}_{task_name}_{step}_{timestamp}"
    full_screenshot: bool = False
    screenshot_mode: str = "auto"
    ssh_profile: str = ""
    ssh_type: str = ""
    evidence_mode: str = ""
    ssh_evidence_mode: str = ""
    ssh_transport: str = ""
    ssh_strategy: str = ""
    per_group_commands: dict[str, str] = Field(default_factory=dict)
    per_group_no_split: dict[str, bool] = Field(default_factory=dict)
    per_group_ssh_profile: dict[str, str] = Field(default_factory=dict)
    per_group_ssh_type: dict[str, str] = Field(default_factory=dict)
    per_group_evidence_mode: dict[str, str] = Field(default_factory=dict)
    per_group_ssh_evidence_mode: dict[str, str] = Field(default_factory=dict)
    per_group_ssh_transport: dict[str, str] = Field(default_factory=dict)
    per_group_ssh_strategy: dict[str, str] = Field(default_factory=dict)
    per_group_timeout: dict[str, int] = Field(default_factory=dict)
    artifact_profile: str = ""
    bmc_artifact_profile: str = ""
    per_group_timeout_seconds: dict[str, int] = Field(default_factory=dict)
    no_split: bool = False


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
    version: str = APP_VERSION
    job_counts: dict[str, int] = Field(default_factory=dict)
    total_jobs: int = 0
    uptime_seconds: float = 0.0


class ExcelPathRequest(BaseModel):
    excelPath: str = Field(..., description="Executor-local .xlsx path to activate")


class ExcelConfigAcceptResponse(BaseModel):
    accepted: bool
    deviceCount: int = 0
    enabledDeviceCount: int = 0
    taskCount: int = 0
    enabledTaskCount: int = 0
    filename: str = ""
    excelHash: str = ""
    sha256: str = ""
    message: str = ""


class LatestConfigResponse(BaseModel):
    hasLatest: bool
    excelHash: str = ""
    filename: str = ""
    sha256: str = ""
    deviceCount: int = 0
    enabledDeviceCount: int = 0
    taskCount: int = 0
    enabledTaskCount: int = 0
    source: str = ""
    createdAt: str = ""
    code: str = ""
    message: str = ""


class PlanRunAcceptResponse(BaseModel):
    accepted: bool
    planId: Union[str, int] = ""
    status: str = ""
    excelHash: str = ""
    message: str = ""
    callbackTransportMode: str = ""


class PlanSummary(BaseModel):
    total: int = 0
    success: int = 0
    failed: int = 0
    in_progress: int = 0
    pending: int = 0
    failureSummary: list[dict[str, Any]] = Field(default_factory=list)
    outputRoot: str = ""


class PlanInfoEvent(BaseModel):
    timestamp: str = ""
    level: str = ""
    message: str = ""


class CheckResultResponse(BaseModel):
    stage: str = ""
    checkId: str = ""
    status: str = ""
    severity: str = ""
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class PlanItemStatusResponse(BaseModel):
    taskId: str = ""
    planItemId: str = ""
    deviceGroup: str = ""
    deviceName: str = ""
    taskName: str = ""
    status: str = ""
    errorMessage: Optional[str] = None
    startedAt: Optional[str] = None
    finishedAt: Optional[str] = None
    infoEvents: list[PlanInfoEvent] = Field(default_factory=list)
    executionStatus: str = ""
    ruleStatus: str = ""
    artifactStatus: str = ""
    readyStatus: str = ""
    checkpointStatus: str = ""
    finalVerdict: str = ""
    checkResults: list[CheckResultResponse] = Field(default_factory=list)


class PlanStatusResponse(BaseModel):
    planId: Union[str, int] = ""
    status: str = ""
    summary: PlanSummary = Field(default_factory=PlanSummary)
    excelHash: str = ""
    outputRoot: str = ""
    startedAt: str = ""
    finishedAt: str = ""
    errorMessage: Optional[str] = None
    infoEvents: list[PlanInfoEvent] = Field(default_factory=list)


class PlanItemsResponse(PlanStatusResponse):
    items: list[PlanItemStatusResponse] = Field(default_factory=list)


class CallbackRetryRequest(BaseModel):
    callbackUrl: str = Field(
        default="",
        description="Optional override URL for retrying pending callbacks",
    )
    mode: str = Field(
        default="batch",
        description="Retry delivery mode: batch or single",
        pattern=r"^(batch|single)$",
    )


class CallbackRetryResponse(BaseModel):
    accepted: bool
    planId: Union[str, int] = ""
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    pendingAfter: int = 0
    status: str = ""
    message: str = ""


class PlanRunCallbackConfig(BaseModel):
    """Callback configuration for plan run status reporting."""
    itemStatusUrl: str = Field(
        default="",
        description="URL to receive plan-item status callbacks",
    )
    planId: Union[str, int] = Field(
        default="",
        description="Business plan ID included in callback payloads",
    )
    mode: str = Field(
        default="batch",
        description="Callback delivery mode: batch or single",
        pattern=r"^(batch|single)$",
    )


class PlanRunRequest(BaseModel):
    """Request body for POST /executor/v1/plans/{plan_id}:run."""
    callback: PlanRunCallbackConfig = Field(
        default_factory=PlanRunCallbackConfig,
        description="Callback configuration for status reporting",
    )
    updater: str = Field(
        default="downstream-system",
        description="Value sent in the updater field of each callback payload",
    )
    runner: str = Field(
        default="fake",
        description="Runner mode: fake executes instantly, real connects to devices",
        pattern=r"^(fake|real)$",
    )


class ExternalPlanRequest(BaseModel):
    """Request body for POST /executor/v1/plans (external plan with excelHash)."""
    excelHash: str = Field(
        default="",
        description="SHA-256 hash of the currently activated Excel config",
    )
    callback: PlanRunCallbackConfig = Field(
        default_factory=PlanRunCallbackConfig,
        description="Callback configuration for status reporting",
    )
    updater: str = Field(
        default="downstream-system",
        description="Value sent in the updater field of each callback payload",
    )
    runner: str = Field(
        default="fake",
        description="Runner mode: fake executes instantly, real connects to devices",
        pattern=r"^(fake|real)$",
    )
