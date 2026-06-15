"""
Retry wrapper for executor calls — AUDIT-007.

Provides execute_with_retry() that wraps executor.execute() in a retry loop.
Only retryable failures trigger retries.  Non-retryable errors (auth failure,
invalid command, rule failure, etc.) shortcut the loop immediately.

Attempt records are stored in ExecutionResult.attempt_records so each
attempt is traceable without creating duplicate result rows.
"""

from __future__ import annotations

import logging
import time

from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult
from ..models.verdict import (
    AttemptRecord,
    is_retryable_failure,
    is_transient_retryable_failure,
)

logger = logging.getLogger("bmc_auto_capture.retry")


def execute_with_retry(executor, plan: TaskPlan, output_root: str) -> ExecutionResult:
    """Execute a plan with retry support.

    Calls executor.execute(plan, output_root) in a loop:
      - Attempt 0 is always the initial execution.
      - If result is SUCCESS → return immediately.
      - If result is a retryable failure and retry_count > 0 → retry.
      - If result is non-retryable → return immediately (no retry).
      - Continues until max_attempts exhausted or success/abort.

    Sets:
      - result.attempt_records (list[AttemptRecord] — full history)
      - result.retry_count (actual number of retries made)
      - plan.retry_attempt (current attempt index before each call)
    """
    max_retries = max(0, plan.task.retry_count)
    result = None
    attempts: list[AttemptRecord] = []
    retry_reasons: list[str] = []

    attempt_idx = 0
    while attempt_idx <= max_retries:
        plan.retry_attempt = attempt_idx
        attempt_start = time.time()

        try:
            result = executor.execute(plan, output_root)
        except BaseException as e:
            # Executor threw an exception — wrap as EXEC_ERROR result
            now = time.time()
            result = ExecutionResult(
                plan_id=plan.plan_id,
                task_id=plan.task_id,
                client_task_id=plan.client_task_id,
                device_name=plan.device.device_name,
                device_group=plan.device.device_group,
                bmc_ip=plan.device.bmc_ip,
                inband_ip=plan.device.inband_ip,
                task_name=plan.task.task_name,
                task_type=plan.task.task_type,
                execution_mode=plan.task.execution_mode,
                execution_status="EXEC_ERROR",
                execution_failure_reason=f"Retry wrapper exception: {e}",
                started_at=attempt_start,
                ended_at=now,
                duration_seconds=round(now - attempt_start, 3),
                endpoint_key=plan.endpoint_key,
                endpoint_type=plan.endpoint_type,
            )

        attempt_end = time.time()

        attempts.append(AttemptRecord(
            attempt_index=attempt_idx,
            max_retries=max_retries,
            execution_status=result.execution_status,
            execution_failure_reason=result.execution_failure_reason or "",
            elapsed_seconds=round(attempt_end - attempt_start, 3),
            started_at=attempt_start,
            ended_at=attempt_end,
            output_dir=result.output_dir,
            artifact_paths=tuple(
                path for path in (
                    *result.screenshots,
                    *result.raw_screenshots,
                    result.html_file,
                    result.txt_file,
                ) if path
            ),
            step_result_count=len(result.step_results),
        ))

        # SUCCESS → done
        if result.execution_status == "EXEC_SUCCESS":
            logger.info(
                "[%s] Attempt %d/%d succeeded",
                plan.device.device_name, attempt_idx + 1, max_retries + 1,
            )
            break

        # Non-retryable → done
        retryable = is_retryable_failure(result)
        if is_transient_retryable_failure(result) and max_retries < 1:
            max_retries = 1
            logger.warning(
                "[%s] transient network error detected; extending max_retries to %d",
                plan.device.device_name,
                max_retries,
            )

        if not retryable:
            logger.info(
                "[%s] Attempt %d/%d not retryable: %s — %s",
                plan.device.device_name, attempt_idx + 1, max_retries + 1,
                result.execution_status,
                (result.execution_failure_reason or "")[:80],
            )
            break

        # Max retries exhausted
        if attempt_idx >= max_retries:
            logger.info(
                "[%s] All %d attempts exhausted: %s",
                plan.device.device_name, max_retries + 1,
                result.execution_status,
            )
            break

        # Retry
        next_delay = min(5.0, 1.0 * (2 ** attempt_idx))
        logger.warning(
            "[%s] Attempt %d/%d failed (retryable=true next_delay=%.1fs): %s — retrying...",
            plan.device.device_name, attempt_idx + 1, max_retries + 1,
            next_delay,
            (result.execution_failure_reason or "")[:80],
        )
        retry_reasons.append(result.execution_failure_reason or result.execution_status)
        time.sleep(next_delay)
        attempt_idx += 1

    # Record attempt history
    if result is not None:
        for attempt in attempts:
            attempt.max_retries = max_retries
        result.attempt_records = attempts
        result.retry_count = max(0, len(attempts) - 1)
        result.attempt_count = len(attempts)
        result.max_attempts = max_retries + 1
        result.final_attempt_index = len(attempts)
        result.retry_reasons = retry_reasons

    return result  # type: ignore[return-value]
