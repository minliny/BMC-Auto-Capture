"""Stable failure categories for reports.

The helpers here intentionally do not change report schemas.  They map noisy
executor text into stable labels that can be placed in existing reason/category
columns.
"""

from __future__ import annotations

from typing import Iterable

from ..models.execution_result import ExecutionResult, StepResult


BMC_FAILURE_CATEGORIES = {
    "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME",
    "BMC_SESSION_EXPIRED",
    "BMC_RELOGIN_FAILED",
    "BMC_PAGE_GOTO_TIMEOUT",
    "BMC_EMPTY_DOM",
    "BMC_PAGE_HEALTH_FAILED",
    "BMC_ACTION_TIMEOUT",
    "BMC_SCREENSHOT_FAILED",
    "BMC_ROUTE_GUARD_STOPPED",
    "BMC_DIALOG_TIMEOUT",
    "BMC_PAGE_CONTEXT_INVALID",
    "ROUTE_GUARD_STOPPED",
}


def classify_failure(result: ExecutionResult) -> str:
    """Return a stable report category, or an empty string when unknown."""
    status = result.execution_status or ""
    reason = (
        result.execution_failure_reason
        or result.rule_failure_reason
        or result.ready_failure_reason
        or result.artifact_failure_reason
        or result._checkpoint_summary()
        or ""
    )
    artifact_reason = result.artifact_failure_reason or ""
    haystack = "\n".join(
        part for part in (
            status,
            reason,
            result.ready_failure_reason or "",
            artifact_reason,
            result.check_failure_summary() if hasattr(result, "check_failure_summary") else "",
            _step_details(result.step_results),
        ) if part
    )
    lower = haystack.lower()
    task_type = (result.task_type or "").upper()
    looks_bmc = task_type == "BMC" or "bmc" in lower

    if "bmc_dependency_missing_playwright_runtime" in lower:
        return "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME"

    if status == "EXEC_SKIPPED_ROUTE_CHANGED" or "route_change" in lower:
        return "BMC_ROUTE_GUARD_STOPPED" if looks_bmc else "ROUTE_GUARD_STOPPED"

    if not looks_bmc:
        return ""

    if "no module named 'playwright.async_api'" in lower or "no module named \"playwright.async_api\"" in lower:
        return "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME"
    if "session runner crashed" in lower and "playwright" in lower:
        return "BMC_DEPENDENCY_MISSING_PLAYWRIGHT_RUNTIME"
    if "re-login failed" in lower or "relogin failed" in lower or "重新登录失败" in lower:
        return "BMC_RELOGIN_FAILED"
    if "bmc_session_invalid" in lower and "failed" in lower:
        return "BMC_RELOGIN_FAILED"
    if "bmc_session_expired" in lower or "session expired" in lower or "会话已过期" in lower:
        return "BMC_SESSION_EXPIRED"
    if "bmc_empty_dom" in lower:
        return "BMC_EMPTY_DOM"
    if "page.goto" in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_PAGE_GOTO_TIMEOUT"
    if "bmc_page_goto_timeout" in lower:
        return "BMC_PAGE_GOTO_TIMEOUT"
    if "goto target_url failed" in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_PAGE_GOTO_TIMEOUT"
    if "bmc页面无法访问" in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_PAGE_GOTO_TIMEOUT"
    if "bmc_dialog_timeout" in lower:
        return "BMC_DIALOG_TIMEOUT"
    if "custom-dialog" in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_DIALOG_TIMEOUT"
    if "timeout dialog" in lower or "登录超时" in lower:
        return "BMC_DIALOG_TIMEOUT"
    if "bmc_page_context_invalid" in lower:
        return "BMC_PAGE_CONTEXT_INVALID"
    if "target page" in lower and ("closed" in lower or "context" in lower):
        return "BMC_PAGE_CONTEXT_INVALID"
    if "browser context" in lower and ("closed" in lower or "invalid" in lower):
        return "BMC_PAGE_CONTEXT_INVALID"
    if "locator." in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_ACTION_TIMEOUT"
    if "required action failed" in lower and ("timeout" in lower or "超时" in lower):
        return "BMC_ACTION_TIMEOUT"
    if "bmc_action_timeout" in lower:
        return "BMC_ACTION_TIMEOUT"
    if "screenshot" in lower and (
        "failed" in lower
        or "missing" in lower
        or "timeout" in lower
        or "截图" in lower
    ):
        return "BMC_SCREENSHOT_FAILED"
    if "bmc_screenshot_failed" in lower:
        return "BMC_SCREENSHOT_FAILED"
    if "bmc_page_health_failed" in lower:
        return "BMC_PAGE_HEALTH_FAILED"

    return ""


def normalized_failure_reason(result: ExecutionResult) -> str:
    """Prefix existing failure reason with the stable category when available."""
    reason = (
        result.execution_failure_reason
        or result.rule_failure_reason
        or result.ready_failure_reason
        or result.artifact_failure_reason
        or result._checkpoint_summary()
        or (result.check_failure_summary() if hasattr(result, "check_failure_summary") else "")
        or ""
    )
    category = classify_failure(result)
    if not category:
        return reason
    if reason.startswith(category):
        return reason
    if reason:
        return f"{category}: {reason}"
    return category


def _step_details(steps: Iterable[StepResult]) -> str:
    details: list[str] = []
    for step in steps or ():
        if getattr(step, "details", ""):
            details.append(str(step.details))
        if getattr(step, "status", ""):
            details.append(str(step.status))
        if getattr(step, "step_name", ""):
            details.append(str(step.step_name))
    return "\n".join(details)
