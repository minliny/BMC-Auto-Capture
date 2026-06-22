"""
Pydantic schemas for executor API request/response validation.
"""

from __future__ import annotations
from typing import Any, Optional, Union
from pydantic import BaseModel, Field

from src._version import APP_VERSION


class ExecutorStatusResponse(BaseModel):
    executor_id: str
    status: str = "ONLINE"
    version: str = APP_VERSION
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
