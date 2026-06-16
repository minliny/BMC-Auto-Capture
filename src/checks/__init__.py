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
    check_result_from_condition,
    check_result_from_checkpoint,
    check_result_from_rule_action,
)

__all__ = [
    "CHECK_FAILED_STATUSES",
    "CheckResult",
    "CheckStage",
    "CheckStatus",
    "append_check_result",
    "check_result_from_condition",
    "check_result_from_checkpoint",
    "check_result_from_rule_action",
]
