"""Interactive retry prompt for failed tasks after a batch run."""

from __future__ import annotations

import sys
import logging
from typing import Callable, Sequence, TextIO

from ..models.execution_result import ExecutionResult

logger = logging.getLogger("bmc_auto_capture.failed_retry")


NON_RETRYABLE_STATUS_PREFIXES = (
    "EXEC_SKIPPED_PRECHECK",
    "EXEC_SKIPPED_PORT",
    "EXEC_SKIPPED_DISABLED",
    "EXEC_SKIPPED_ROUTE_CHANGED",
    "EXEC_SKIPPED_STOPPED",
)


def is_failed_result(result: ExecutionResult) -> bool:
    status = result.execution_status or ""
    if status != "EXEC_SUCCESS":
        return True
    if result.rule_status in ("RULE_FAILED", "RULE_PARSE_FAILED"):
        return True
    if result.checkpoint_status == "CHECK_FAIL":
        return True
    if result.artifact_status == "ARTIFACT_FAILED":
        return True
    if result.final_verdict == "FAIL":
        return True
    return False


def is_retryable_failed_result(result: ExecutionResult) -> bool:
    if not is_failed_result(result):
        return False
    status = result.execution_status or ""
    return not status.startswith(NON_RETRYABLE_STATUS_PREFIXES)


def is_failed_for_exit(result: ExecutionResult) -> bool:
    return is_failed_result(result)


def failed_result_count(results: Sequence[ExecutionResult]) -> int:
    return sum(1 for result in results if is_failed_for_exit(result))


def prompt_retry_failed_tasks(
    app,
    results: Sequence[ExecutionResult],
    *,
    mode: str,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    stdin: TextIO | None = None,
) -> list[ExecutionResult]:
    """Offer one post-summary retry for failed tasks when running interactively."""
    result_list = list(results)
    failed_total = failed_result_count(result_list)
    if failed_total <= 0:
        return result_list

    stream = sys.stdin if stdin is None else stdin
    if not getattr(stream, "isatty", lambda: False)():
        return result_list

    retryable_result_total = sum(1 for result in result_list if is_retryable_failed_result(result))
    retry_plans = app.failed_retry_candidates(result_list)
    retryable_total = len(retry_plans)
    non_retryable_total = max(0, failed_total - retryable_result_total)
    unresolved_retryable_total = max(0, retryable_result_total - retryable_total)

    output_func("")
    output_func(
        f"  失败任务: {failed_total} 个  "
        f"可重试: {retryable_total} 个  "
        f"不可重试: {non_retryable_total} 个"
    )
    if unresolved_retryable_total:
        output_func(f"  {unresolved_retryable_total} 个失败任务无法回溯到原始计划，已排除重试。")
    if not retry_plans:
        output_func("  存在失败但无可重试任务。")
        return result_list

    choice = input_func("  输入 0 只重试可重试失败任务，按 Enter 结束: ").strip()
    if choice != "0":
        return result_list

    original_output_root = getattr(getattr(app, "config", None), "output_root", "")
    original_stop_metadata = {}
    if hasattr(app, "current_stop_metadata"):
        original_stop_metadata = dict(app.current_stop_metadata())
    elif hasattr(app, "_stop_metadata"):
        original_stop_metadata = dict(app._stop_metadata())
    retry_results = app.retry_failed_tasks(result_list, mode=mode)
    if not retry_results:
        output_func("  未找到可重试的失败任务。")
        return result_list

    output_func(f"  失败任务重试完成: {len(retry_results)} 个")
    merged_results = merge_retry_results(result_list, retry_results)
    if hasattr(app, "replace_results_after_retry"):
        app.replace_results_after_retry(merged_results)
    if hasattr(app, "write_retry_merged_reports"):
        merged_report_dir = app.write_retry_merged_reports(
            merged_results,
            output_dir=original_output_root,
            stop_metadata=original_stop_metadata,
        )
        output_func(f"  重试后合并报告: {merged_report_dir}")
    return merged_results


def merge_retry_results(
    original_results: Sequence[ExecutionResult],
    retry_results: Sequence[ExecutionResult],
) -> list[ExecutionResult]:
    """Replace original failed rows with their retry result for exit decisions."""
    retry_list = list(retry_results)
    retry_by_key: dict[tuple, ExecutionResult] = {}
    used_keys: set[tuple] = set()

    for result in retry_list:
        for key in result_identity_keys(result):
            if key not in retry_by_key:
                retry_by_key[key] = result

    merged: list[ExecutionResult] = []
    used_retry_ids: set[int] = set()
    for result in original_results:
        replacement = None
        replacement_key = None
        if is_failed_result(result):
            for key in result_identity_keys(result):
                candidate = retry_by_key.get(key)
                if (
                    candidate is not None
                    and key not in used_keys
                    and id(candidate) not in used_retry_ids
                ):
                    replacement = candidate
                    replacement_key = key
                    break
        if replacement is None:
            merged.append(result)
            continue
        merged.append(replacement)
        used_retry_ids.add(id(replacement))
        if replacement_key is not None:
            used_keys.add(replacement_key)

    unmatched_count = sum(1 for result in retry_list if id(result) not in used_retry_ids)
    if unmatched_count:
        logger.warning(
            "Retry merge ignored %d unmatched retry results; original totals preserved",
            unmatched_count,
        )
    return merged


def result_identity_keys(result: ExecutionResult) -> list[tuple]:
    keys: list[tuple] = []
    plan_item_id = getattr(result, "plan_item_id", "")
    if plan_item_id:
        keys.append(("plan_item_id", plan_item_id))
    if result.task_id:
        keys.append((
            "plan_device_task_id",
            str(result.plan_id),
            result.device_name,
            result.task_id,
        ))
    if result.client_task_id:
        keys.append((
            "plan_device_client_task_id",
            str(result.plan_id),
            result.device_name,
            result.client_task_id,
        ))
    if result.task_name:
        keys.append((
            "plan_device_task_name",
            str(result.plan_id),
            result.device_name,
            result.task_name,
        ))
    return keys


def plan_identity_keys(plan) -> list[tuple]:
    keys: list[tuple] = []
    plan_item_id = (
        getattr(plan, "effective_plan_item_id", "")
        or getattr(plan, "plan_item_id", "")
    )
    if plan_item_id:
        keys.append(("plan_item_id", plan_item_id))
    effective_task_id = getattr(plan, "effective_task_id", "") or getattr(plan, "task_id", "")
    if effective_task_id:
        keys.append((
            "plan_device_task_id",
            str(getattr(plan, "plan_id", "")),
            plan.device.device_name,
            effective_task_id,
        ))
    client_task_id = getattr(plan, "client_task_id", "")
    if client_task_id:
        keys.append((
            "plan_device_client_task_id",
            str(getattr(plan, "plan_id", "")),
            plan.device.device_name,
            client_task_id,
        ))
    if plan.task.task_name:
        keys.append((
            "plan_device_task_name",
            str(getattr(plan, "plan_id", "")),
            plan.device.device_name,
            plan.task.task_name,
        ))
    return keys
