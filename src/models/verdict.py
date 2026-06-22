"""
Verdict computation and retryability — shared by App, DynamicScheduler, and SessionRunner.

AUDIT-008: All ExecutionResults must have a non-empty final_verdict.
AUDIT-007: is_retryable_failure drives retry decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..checks.models import CHECK_FAILED_STATUSES, CheckResult, CheckStage
from .execution_result import ExecutionResult

# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

# Precedence: FAIL > SKIPPED > WARN > PASS
#
# FAIL conditions:
#   - EXEC_FAILED, EXEC_ERROR, EXEC_TIMEOUT, EXEC_SUCCESS_RULE_FAILED
#   - ARTIFACT_FAILED
#   - RULE_FAILED / RULE_PARSE_FAILED
#   - CHECK_FAIL
#
# SKIPPED conditions:
#   - any execution_status starting with "EXEC_SKIPPED"
#   - PRECHECK_SKIPPED
#
# WARN conditions:
#   - EXEC_PARTIAL
#   - READY_NOT_READY
#   - CHECK_WARN
#   - ARTIFACT_PARTIAL
#
# PASS: everything else


def compute_verdict(result: ExecutionResult) -> str:
    """Compute final_verdict from execution/artifact/checkpoint status."""
    status = result.execution_status
    known_statuses = {
        "EXEC_SUCCESS",
        "EXEC_FAILED",
        "EXEC_ERROR",
        "EXEC_TIMEOUT",
        "EXEC_SUCCESS_RULE_FAILED",
        "EXEC_PARTIAL",
        "EXEC_BLOCKED",
        "PRECHECK_SKIPPED",
    }
    if isinstance(status, str) and status.startswith("EXEC_SKIPPED"):
        known_statuses.add(status)

    if not isinstance(status, str) or status not in known_statuses:
        result.unknown_status = True
        result.raw_execution_status = "" if status is None else str(status)
        return "FAIL"

    result.unknown_status = False
    result.raw_execution_status = ""

    # FAIL conditions
    if status in ("EXEC_FAILED", "EXEC_ERROR", "EXEC_TIMEOUT", "EXEC_SUCCESS_RULE_FAILED"):
        return "FAIL"
    if _has_blocking_check_failure(result):
        return "FAIL"
    if result.artifact_status == "ARTIFACT_FAILED":
        return "FAIL"
    if result.rule_status in ("RULE_FAILED", "RULE_PARSE_FAILED"):
        return "FAIL"
    if result.checkpoint_status == "CHECK_FAIL":
        return "FAIL"

    # SKIPPED conditions
    if status.startswith("EXEC_SKIPPED") or status == "PRECHECK_SKIPPED":
        return "SKIPPED"
    if status == "EXEC_BLOCKED":
        return "BLOCKED"

    # WARN conditions
    if status == "EXEC_PARTIAL":
        return "WARN"
    if result.ready_status == "READY_NOT_READY":
        return "WARN"
    if _has_check_warning(result):
        return "WARN"
    if result.checkpoint_status == "CHECK_WARN":
        return "WARN"
    if result.artifact_status == "ARTIFACT_PARTIAL":
        return "WARN"

    return "PASS" if status == "EXEC_SUCCESS" else "FAIL"


def _iter_check_results(result: ExecutionResult):
    for cr in getattr(result, "check_results", None) or ():
        if isinstance(cr, CheckResult):
            yield cr
        elif isinstance(cr, dict):
            yield CheckResult.from_dict(cr)


def _has_blocking_check_failure(result: ExecutionResult) -> bool:
    for cr in _iter_check_results(result):
        if cr.stage == CheckStage.POST_AUDIT:
            continue
        if cr.stage == CheckStage.READY and not _check_blocks_final(cr):
            continue
        if cr.status in CHECK_FAILED_STATUSES and cr.severity == "ERROR":
            return True
    return False


def _has_check_warning(result: ExecutionResult) -> bool:
    for cr in _iter_check_results(result):
        if cr.stage == CheckStage.POST_AUDIT:
            continue
        if cr.status == "WARN":
            return True
        if cr.stage == CheckStage.READY and cr.status in CHECK_FAILED_STATUSES:
            return True
        if cr.status in CHECK_FAILED_STATUSES and cr.severity == "WARNING":
            return True
    return False


def _check_blocks_final(check: CheckResult) -> bool:
    details = check.details or {}
    priority = str(details.get("priority", "") or "").upper()
    effect = str(details.get("effect_on_final", "") or "").lower()
    return priority == "P0" or effect == "fail"


# ---------------------------------------------------------------------------
# Retryability — AUDIT-007
# ---------------------------------------------------------------------------

RETRYABLE_STATUSES = frozenset({"EXEC_TIMEOUT", "EXEC_FAILED"})
TRANSIENT_RETRYABLE_REASON_PATTERNS: list[str] = [
    "WinError 10054",
    "10054",
    "connection reset",
    "Connection reset",
    "forcibly closed",
    "远程主机强迫关闭",
    "remote host closed",
    "connection aborted",
    "Connection aborted",
    "ECONNRESET",
]

# Patterns that indicate non-retryable failures even when status is retryable
NON_RETRYABLE_REASON_PATTERNS: list[str] = [
    "认证失败",
    "Authentication",
    "密码错误",
    "IP为空",
    "IP empty",
    "COMMAND_MISSING",
    "COMMAND_OUTPUT_MISSING",
    "ONLY_LOGIN_BANNER",
    "规则检查失败",
    "JSON 解析失败",
    "JSON schema invalid",
    "CONFIG_INVALID",
    "Path escape",
    "Unsafe path component",
    "BMC_SESSION_PREEMPTED",
    "Unsupported protocol",
    "SSH认证失败",
    "SSH错误: ChannelException",
    "too many authentication",
]


def is_retryable_failure(result: ExecutionResult) -> bool:
    """Return True if this result represents a retryable failure.

    A failure is retryable when:
      1. The execution_status is in RETRYABLE_STATUSES, AND
      2. The failure_reason does NOT match any NON_RETRYABLE_REASON_PATTERNS.
    """
    reason = result.execution_failure_reason or ""
    for pattern in NON_RETRYABLE_REASON_PATTERNS:
        if pattern in reason:
            return False
    if is_transient_retryable_failure(result):
        return True
    if result.execution_status not in RETRYABLE_STATUSES:
        return False
    return True


def is_transient_retryable_failure(result: ExecutionResult) -> bool:
    """Return True for transient network/socket failures worth retrying."""
    if result.execution_status not in ("EXEC_ERROR", "EXEC_FAILED", "EXEC_TIMEOUT"):
        return False
    reason = result.execution_failure_reason or ""
    for pattern in NON_RETRYABLE_REASON_PATTERNS:
        if pattern in reason:
            return False
    return any(pattern in reason for pattern in TRANSIENT_RETRYABLE_REASON_PATTERNS)


@dataclass
class AttemptRecord:
    """Record of a single execution attempt (initial + retries)."""
    attempt_index: int = 0       # 0-based
    max_retries: int = 0         # from task.retry_count
    execution_status: str = ""
    execution_failure_reason: str = ""
    elapsed_seconds: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    output_dir: str = ""
    artifact_paths: tuple[str, ...] = ()
    step_result_count: int = 0
