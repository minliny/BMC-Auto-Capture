"""
Pydantic schemas for API request/response validation.
"""


from __future__ import annotations
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


class ExecuteStartRequest(BaseModel):
    excel_path: str
    config_path: Optional[str] = None


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
    plan_id: str
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
