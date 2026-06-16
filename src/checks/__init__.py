"""Unified check result primitives.

This package is intentionally small: it standardizes how existing checks
report results without forcing every checker into one rule language.
"""

from .models import (
    CHECK_FAILED_STATUSES,
    CheckResult,
    CheckStage,
    CheckStatus,
    append_check_result,
    check_result_from_artifact_status,
    check_result_from_condition,
    check_result_from_checkpoint,
    check_result_from_execution_status,
    check_result_from_health_result,
    check_result_from_rule_action,
    check_result_from_validation_message,
    check_results_from_validation_report,
)

__all__ = [
    "CHECK_FAILED_STATUSES",
    "CheckResult",
    "CheckStage",
    "CheckStatus",
    "append_check_result",
    "check_result_from_artifact_status",
    "check_result_from_condition",
    "check_result_from_checkpoint",
    "check_result_from_execution_status",
    "check_result_from_health_result",
    "check_result_from_rule_action",
    "check_result_from_validation_message",
    "check_results_from_validation_report",
]
