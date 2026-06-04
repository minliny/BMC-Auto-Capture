"""
Pydantic schemas for API request/response validation.
"""


from __future__ import annotations
import re
from pydantic import BaseModel, Field
from typing import Optional


class ValidationMessage(BaseModel):
    level: str
    source: str
    row: int
    field: str
    message: str


class UploadResponse(BaseModel):
    status: str
    device_count: int
    device_enabled_count: int
    task_count: int
    task_enabled_count: int
    errors: list[ValidationMessage] = []
    warnings: list[ValidationMessage] = []


EXEC_ID_RE = r"^[A-Za-z0-9._:-]{1,64}$"


def validate_execution_id(exec_id: str) -> str | None:
    """Validate client-supplied execution_id. Returns error message or None."""
    if not exec_id or not exec_id.strip():
        return "execution_id must not be empty"
    if len(exec_id) > 64:
        return "execution_id must be <= 64 characters"
    if "/" in exec_id:
        return "execution_id must not contain '/'"
    if "\\" in exec_id:
        return "execution_id must not contain '\\'"
    if ".." in exec_id:
        return "execution_id must not contain '..'"
    if not bool(re.match(r"^[A-Za-z0-9._:-]{1,64}$", exec_id)):
        return "execution_id contains invalid characters (allowed: A-Za-z0-9._:-)"
    return None


class ExecuteStartRequest(BaseModel):
    execution_id: Optional[str] = Field(None, description="Client-supplied execution ID (max 64 chars, A-Za-z0-9._:-)")
    excel_path: Optional[str] = None
    config_path: Optional[str] = None
    plans_json: Optional[str] = Field(None, description="Optional JSON array of in-memory plans to execute directly, bypassing Excel")


class ExecuteStartResponse(BaseModel):
    execution_id: str
    plan_count: int
    message: str


class ExecutionStatus(BaseModel):
    execution_id: str
    phase: str
    completed: int
    total: int
    is_running: bool
    is_paused: bool


class PlanStatusEvent(BaseModel):
    execution_id: str = ""
    plan_id: str
    task_id: str = ""
    client_task_id: str = ""
    device_name: str
    task_name: str
    status: str
    index: int
    total: int


class ScreenshotInfo(BaseModel):
    plan_id: str
    device_name: str
    task_name: str
    screenshot_paths: list[str]


class SummaryResponse(BaseModel):
    total: int
    success: int
    failed: int
    error: int
    skipped_preflight: int
    skipped_port_blocked: int
    skipped_route: int
    rule_passed: int
    rule_failed: int


class TaskPushRequest(BaseModel):
    tasks_json: str = Field(..., description="JSON string of tasks array")
    mode: str = Field("merge", description="'merge' or 'replace'")


class WebhookRegisterRequest(BaseModel):
    url: str
    events: list[str]
    secret: str = ""


class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    secret: str = ""
    enabled: bool = True
    created_at: float = 0.0
