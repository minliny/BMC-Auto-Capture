"""
ResultCollector — merges ExecutionResult list into result.csv + final_result.csv.
"""

import csv
import logging
import os
from collections import Counter
from typing import Sequence

from ..models.execution_result import ExecutionResult
from ..models.verdict import compute_verdict
from ..utils.path_safety import safe_join_under_root, is_safe_path_component
from .failure_classification import normalized_failure_reason

logger = logging.getLogger("bmc_auto_capture.collector")


def _ensure_final_verdict(result: ExecutionResult) -> None:
    if not result.final_verdict:
        result.final_verdict = compute_verdict(result)


def _csv_row_with_normalized_reason(result: ExecutionResult) -> list:
    row = result.to_csv_row()
    # Keep the existing CSV schema; make the failure reason itself actionable.
    row[12] = normalized_failure_reason(result)
    return row


def write_result_csv(results: Sequence[ExecutionResult], output_dir: str, filename: str = "result.csv") -> str:
    """Write all results to a single CSV file."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(ExecutionResult.csv_header())
        for r in sorted(results, key=lambda r: (r.device_group, r.device_name, r.task_name)):
            _ensure_final_verdict(r)
            writer.writerow(_csv_row_with_normalized_reason(r))

    logger.info("Wrote %d results to %s", len(results), path)
    return path


def write_final_result_csv(results: Sequence[ExecutionResult], output_dir: str, filename: str = "final_result.csv") -> str:
    """Write final_result.csv — same data, sorted differently for reporting."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(ExecutionResult.csv_header())
        for r in sorted(results, key=lambda r: (r.execution_status, r.device_name, r.task_name)):
            _ensure_final_verdict(r)
            writer.writerow(_csv_row_with_normalized_reason(r))

    logger.info("Wrote final_result.csv to %s", path)
    return path


def compute_summary(results: Sequence[ExecutionResult]) -> dict:
    """Return aggregate counts. Covers ALL statuses for summary closure."""
    total = len(results)
    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    failed = sum(1 for r in results if r.execution_status == "EXEC_FAILED")
    error = sum(1 for r in results if r.execution_status == "EXEC_ERROR")
    timeout = sum(1 for r in results if r.execution_status == "EXEC_TIMEOUT")
    partial = sum(1 for r in results if r.execution_status == "EXEC_PARTIAL")
    skipped_preflight = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_PRECHECK_FAILED")
    skipped_port = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_PORT_BLOCKED")
    skipped_route = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_ROUTE_CHANGED")
    skipped_stopped = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_STOPPED")
    skipped_disabled = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_DISABLED")
    skipped_session = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_SESSION_FAILED")
    skipped_session_failure = sum(
        1 for r in results if r.execution_status == "EXEC_SKIPPED_SESSION_FAILURE"
    )
    blocked = sum(1 for r in results if r.execution_status == "EXEC_BLOCKED")
    precheck_skipped = sum(1 for r in results if r.execution_status == "PRECHECK_SKIPPED")
    known_statuses = {
        "EXEC_SUCCESS", "EXEC_FAILED", "EXEC_ERROR", "EXEC_TIMEOUT", "EXEC_PARTIAL",
        "EXEC_SKIPPED_PRECHECK_FAILED", "EXEC_SKIPPED_PORT_BLOCKED",
        "EXEC_SKIPPED_ROUTE_CHANGED", "EXEC_SKIPPED_STOPPED",
        "EXEC_SKIPPED_DISABLED", "EXEC_SKIPPED_SESSION_FAILED",
        "EXEC_SKIPPED_SESSION_FAILURE", "EXEC_BLOCKED", "PRECHECK_SKIPPED",
    }
    unknown_statuses = Counter(
        r.execution_status for r in results if r.execution_status not in known_statuses
    )

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "error": error,
        "timeout": timeout,
        "partial": partial,
        "skipped_preflight": skipped_preflight,
        "skipped_port_blocked": skipped_port,
        "skipped_route": skipped_route,
        "skipped_stopped": skipped_stopped,
        "skipped_disabled": skipped_disabled,
        "skipped_session": skipped_session + skipped_session_failure,
        "blocked": blocked,
        "precheck_skipped": precheck_skipped,
        "unknown": sum(unknown_statuses.values()),
        "unknown_statuses": dict(unknown_statuses),
        "rule_passed": sum(1 for r in results if r.rule_status == "RULE_PASSED"),
        "rule_failed": sum(1 for r in results if r.rule_status == "RULE_FAILED"),
        "checkpoint_pass": sum(1 for r in results if r.checkpoint_status == "CHECK_PASS"),
        "checkpoint_fail": sum(1 for r in results if r.checkpoint_status == "CHECK_FAIL"),
        "checkpoint_warn": sum(1 for r in results if r.checkpoint_status == "CHECK_WARN"),
        "checkpoint_skip": sum(1 for r in results if r.checkpoint_status == "CHECK_SKIP"),
        "final_pass": sum(1 for r in results if r.final_verdict == "PASS"),
        "final_fail": sum(1 for r in results if r.final_verdict == "FAIL"),
        "final_warn": sum(1 for r in results if r.final_verdict == "WARN"),
        "final_skipped": sum(1 for r in results if r.final_verdict == "SKIPPED"),
        "final_blocked": sum(1 for r in results if r.final_verdict == "BLOCKED"),
    }


def write_preflight_auth_csv(report, output_dir: str,
                             filename: str = "auth_check_result.csv") -> str:
    """Write credential auth check results to CSV."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    header = ["device_name", "device_group", "target", "endpoint",
              "username", "status", "reason", "duration_seconds"]

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in sorted(report.results, key=lambda x: x.device_name):
            # BMC row
            bmc_status = getattr(r, 'bmc_status', '')
            if bmc_status:
                writer.writerow([
                    r.device_name, "", "BMC",
                    getattr(r, 'bmc_endpoint', ''),
                    getattr(r, 'bmc_username', ''),
                    bmc_status,
                    getattr(r, 'bmc_error', ''),
                    str(round(getattr(r, 'bmc_duration', 0), 1)),
                ])
            # SSH row
            ssh_status = getattr(r, 'ssh_status', '')
            if ssh_status:
                writer.writerow([
                    r.device_name, "", "SSH",
                    getattr(r, 'ssh_endpoint', ''),
                    getattr(r, 'ssh_username', ''),
                    ssh_status,
                    getattr(r, 'ssh_error', ''),
                    str(round(getattr(r, 'ssh_duration', 0), 1)),
                ])

    logger.info("Wrote auth_check_result.csv to %s (%d rows)", path, len(report.results) * 2)
    return path
