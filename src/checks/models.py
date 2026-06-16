"""Unified check result model used across execution stages.

The goal is to normalize outputs from existing checkers.  The checker
implementation can stay protocol-specific; the result shape should not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any


class CheckStage:
    CONFIG = "CONFIG_CHECK"
    PRECHECK = "PRECHECK"
    SESSION = "SESSION_CHECK"
    EXECUTION = "EXECUTION_CHECK"
    READY = "READY_CHECK"
    ARTIFACT = "ARTIFACT_CHECK"
    RESULT = "RESULT_CHECK"
    POST_AUDIT = "POST_AUDIT"


class CheckStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    ERROR = "ERROR"


CHECK_FAILED_STATUSES = {CheckStatus.FAIL, CheckStatus.ERROR}


@dataclass
class CheckResult:
    stage: str
    check_id: str
    status: str
    severity: str = "ERROR"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    target: str = ""
    actual: str = ""
    evidence_ref: str = ""
    evaluated_at: float = 0.0

    def __post_init__(self) -> None:
        self.stage = str(self.stage or "").upper()
        self.check_id = str(self.check_id or "")
        self.status = normalize_check_status(self.status)
        self.severity = normalize_severity(self.severity)
        if self.evaluated_at == 0.0:
            self.evaluated_at = time.time()

    @property
    def is_failure(self) -> bool:
        return self.status in CHECK_FAILED_STATUSES

    @property
    def is_warning(self) -> bool:
        return self.status == CheckStatus.WARN

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["details"]:
            data.pop("details", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckResult":
        return cls(
            stage=str(data.get("stage", "")),
            check_id=str(data.get("check_id", data.get("checkId", ""))),
            status=str(data.get("status", "")),
            severity=str(data.get("severity", "ERROR")),
            message=str(data.get("message", "")),
            details=dict(data.get("details", {}) or {}),
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            actual=str(data.get("actual", "")),
            evidence_ref=str(data.get("evidence_ref", data.get("evidenceRef", ""))),
            evaluated_at=float(data.get("evaluated_at", data.get("evaluatedAt", 0.0)) or 0.0),
        )


def normalize_check_status(status: object) -> str:
    raw = str(status or "").strip().upper()
    mapping = {
        "SUCCESS": CheckStatus.PASS,
        "PASSED": CheckStatus.PASS,
        "CHECK_PASS": CheckStatus.PASS,
        "READY": CheckStatus.PASS,
        "FAILED": CheckStatus.FAIL,
        "FAILURE": CheckStatus.FAIL,
        "CHECK_FAIL": CheckStatus.FAIL,
        "CHECK_WARN": CheckStatus.WARN,
        "WARNING": CheckStatus.WARN,
        "CHECK_SKIP": CheckStatus.SKIP,
        "SKIPPED": CheckStatus.SKIP,
    }
    return mapping.get(raw, raw if raw in {"PASS", "FAIL", "WARN", "SKIP", "ERROR"} else CheckStatus.ERROR)


def normalize_severity(severity: object) -> str:
    raw = str(severity or "ERROR").strip().upper()
    if raw in {"ERROR", "WARNING", "INFO"}:
        return raw
    if raw == "WARN":
        return "WARNING"
    return "ERROR"


def append_check_result(result: Any, check_result: CheckResult) -> None:
    """Append a check result to an ExecutionResult-like object."""
    if result is None:
        return
    checks = getattr(result, "check_results", None)
    if checks is None:
        checks = []
        setattr(result, "check_results", checks)
    checks.append(check_result)


def check_result_from_condition(
    condition: Any,
    *,
    stage: str,
    check_id_prefix: str,
    severity: str = "ERROR",
    source: str = "",
) -> CheckResult:
    condition_type = str(getattr(condition, "condition_type", "") or "condition")
    status = normalize_check_status(getattr(condition, "status", "ERROR"))
    target = str(getattr(condition, "target", "") or "")
    actual = str(getattr(condition, "actual", "") or "")
    details = str(getattr(condition, "details", "") or "")
    message = details or actual or target
    return CheckResult(
        stage=stage,
        check_id=f"{check_id_prefix}.{condition_type}",
        status=status,
        severity=severity,
        message=message,
        details={
            "condition_type": condition_type,
            "target": target,
            "actual": actual,
            "details": details,
        },
        source=source,
        target=target,
        actual=actual,
    )


def check_result_from_checkpoint(
    checkpoint: Any,
    *,
    stage: str = CheckStage.RESULT,
    source: str = "checkpoint",
) -> CheckResult:
    name = str(getattr(checkpoint, "checkpoint_name", "") or "checkpoint")
    status = normalize_check_status(getattr(checkpoint, "status", "ERROR"))
    details = str(getattr(checkpoint, "details", "") or "")
    evidence_ref = str(getattr(checkpoint, "evidence_ref", "") or "")
    return CheckResult(
        stage=stage,
        check_id=f"{source}.{name}",
        status=status,
        severity="ERROR",
        message=details,
        details={"checkpoint_name": name, "details": details},
        source=source,
        evidence_ref=evidence_ref,
        evaluated_at=float(getattr(checkpoint, "evaluated_at", 0.0) or 0.0),
    )


def check_result_from_rule_action(
    action_result: Any,
    *,
    stage: str,
    rule_scope: str,
    severity: str = "ERROR",
) -> CheckResult:
    action_type = str(getattr(action_result, "action_type", "") or "rule_action")
    status = normalize_check_status(getattr(action_result, "status", "ERROR"))
    message = str(getattr(action_result, "message", "") or "")
    return CheckResult(
        stage=stage,
        check_id=f"{rule_scope}.{action_type}",
        status=status,
        severity=severity,
        message=message,
        details={"action_type": action_type, "message": message},
        source=rule_scope,
    )


def check_result_from_health_result(
    health: Any,
    *,
    source: str = "bmc.health",
    stage: str = CheckStage.SESSION,
) -> CheckResult:
    health_stage = str(getattr(health, "stage", "") or "health")
    healthy = bool(getattr(health, "healthy", False))
    status = CheckStatus.PASS if healthy else CheckStatus.FAIL
    health_status = str(getattr(health, "status", "") or "")
    details_text = str(getattr(health, "details", "") or health_status)
    return CheckResult(
        stage=stage,
        check_id=f"{source}.{health_stage}",
        status=status,
        severity="ERROR",
        message=details_text or ("health OK" if healthy else "health failed"),
        details={
            "health_stage": health_stage,
            "health_status": health_status,
            "matched_keyword": str(getattr(health, "matched_keyword", "") or ""),
            "url": str(getattr(health, "url", "") or ""),
            "title": str(getattr(health, "title", "") or ""),
            "html_size": int(getattr(health, "html_size", 0) or 0),
            "recoverable": bool(getattr(health, "recoverable", False)),
            "terminal": bool(getattr(health, "terminal", False)),
        },
        source=source,
        actual=health_status,
    )


def check_result_from_artifact_status(
    artifact_status: str,
    reason: str = "",
    *,
    source: str = "artifact",
    evidence_ref: str = "",
) -> CheckResult:
    status_map = {
        "ARTIFACT_SAVED": CheckStatus.PASS,
        "ARTIFACT_PARTIAL": CheckStatus.WARN,
        "ARTIFACT_FAILED": CheckStatus.FAIL,
        "ARTIFACT_PENDING": CheckStatus.SKIP,
    }
    normalized_artifact_status = str(artifact_status or "ARTIFACT_PENDING")
    return CheckResult(
        stage=CheckStage.ARTIFACT,
        check_id=f"{source}.{normalized_artifact_status.lower()}",
        status=status_map.get(normalized_artifact_status, CheckStatus.ERROR),
        severity="ERROR",
        message=reason or normalized_artifact_status,
        details={
            "artifact_status": normalized_artifact_status,
            "reason": reason,
        },
        source=source,
        evidence_ref=evidence_ref,
    )


def check_result_from_execution_status(
    execution_status: str,
    reason: str = "",
    *,
    stage: str = CheckStage.EXECUTION,
    check_id: str = "execution.status",
    source: str = "execution",
) -> CheckResult:
    normalized_status = str(execution_status or "")
    if normalized_status == "EXEC_SUCCESS":
        status = CheckStatus.PASS
    elif normalized_status in {"EXEC_PARTIAL"}:
        status = CheckStatus.WARN
    elif normalized_status.startswith("EXEC_SKIPPED") or normalized_status == "PRECHECK_SKIPPED":
        status = CheckStatus.SKIP
    else:
        status = CheckStatus.FAIL
    return CheckResult(
        stage=stage,
        check_id=check_id,
        status=status,
        severity="ERROR",
        message=reason or normalized_status,
        details={
            "execution_status": normalized_status,
            "reason": reason,
        },
        source=source,
        actual=normalized_status,
    )
