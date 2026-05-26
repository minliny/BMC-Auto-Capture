"""
ResultCollector — merges ExecutionResult list into result.csv + final_result.csv.
"""

import csv
import logging
import os
from typing import Sequence

from ..models.execution_result import ExecutionResult

logger = logging.getLogger("bmc_auto_capture.collector")


def write_result_csv(results: Sequence[ExecutionResult], output_dir: str, filename: str = "result.csv") -> str:
    """Write all results to a single CSV file."""
    path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(ExecutionResult.csv_header())
        for r in sorted(results, key=lambda r: (r.device_group, r.device_name, r.task_name)):
            writer.writerow(r.to_csv_row())

    logger.info("Wrote %d results to %s", len(results), path)
    return path


def write_final_result_csv(results: Sequence[ExecutionResult], output_dir: str, filename: str = "final_result.csv") -> str:
    """Write final_result.csv — same data, sorted differently for reporting."""
    path = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(ExecutionResult.csv_header())
        for r in sorted(results, key=lambda r: (r.execution_status, r.device_name, r.task_name)):
            writer.writerow(r.to_csv_row())

    logger.info("Wrote final_result.csv to %s", path)
    return path


def compute_summary(results: Sequence[ExecutionResult]) -> dict:
    """Return aggregate counts."""
    total = len(results)
    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    failed = sum(1 for r in results if r.execution_status == "EXEC_FAILED")
    error = sum(1 for r in results if r.execution_status == "EXEC_ERROR")
    skipped_preflight = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_PRECHECK_FAILED")
    skipped_port = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_PORT_BLOCKED")
    skipped_route = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_ROUTE_CHANGED")
    skipped_disabled = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_DISABLED")

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "error": error,
        "skipped_preflight": skipped_preflight,
        "skipped_port_blocked": skipped_port,
        "skipped_route": skipped_route,
        "skipped_disabled": skipped_disabled,
        "rule_passed": sum(1 for r in results if r.rule_status == "RULE_PASSED"),
        "rule_failed": sum(1 for r in results if r.rule_status == "RULE_FAILED"),
    }
