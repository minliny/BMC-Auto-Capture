"""
Timing report writer — plan_timing.csv, device_timing.csv, endpoint_timing.csv, execution_summary.json
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from collections import defaultdict
from typing import Sequence

from ..models.execution_result import ExecutionResult
from ..utils.path_safety import safe_join_under_root, is_safe_path_component

logger = logging.getLogger("bmc_auto_capture.timing")


def write_plan_timing_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "plan_timing.csv",
) -> str:
    """Write per-plan timing to CSV."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    header = [
        "execution_id", "plan_id", "device_name", "device_group",
        "task_name", "task_type",
        "endpoint_key", "endpoint_type",
        "status",
        "ready_at", "resource_wait_started_at", "resource_acquired_at",
        "executor_started_at", "executor_finished_at", "ended_at",
        "duration_seconds", "resource_wait_seconds", "executor_duration_seconds",
        "retry_count", "output_dir",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in sorted(results, key=lambda x: (x.device_group, x.device_name, x.task_name)):
            writer.writerow([
                r.plan_id,  # execution_id not yet wired — use plan_id as proxy
                r.plan_id,
                r.device_name,
                r.device_group,
                r.task_name,
                r.task_type,
                r.endpoint_key,
                r.endpoint_type,
                r.execution_status,
                _fmt_ts(r.started_at),
                "",  # resource_wait_started_at — not yet on ExecutionResult
                "",  # resource_acquired_at
                _fmt_ts(r.started_at),
                _fmt_ts(r.ended_at),
                _fmt_ts(r.ended_at),
                str(round(r.duration_seconds, 1)),
                str(round(r.resource_wait_seconds, 1)),
                str(round(r.executor_duration_seconds, 1)),
                str(r.retry_count),
                r.output_dir,
            ])

    logger.info("Wrote plan_timing.csv to %s (%d rows)", path, len(results))
    return path


def write_device_timing_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "device_timing.csv",
) -> str:
    """Write per-device aggregated timing to CSV."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    # Group by device_name + device_group
    groups: dict[tuple[str, str], list[ExecutionResult]] = defaultdict(list)
    for r in results:
        groups[(r.device_name, r.device_group)].append(r)

    header = [
        "device_name", "device_group",
        "total_tasks", "success", "failed",
        "wall_clock_seconds",
        "sum_plan_duration_seconds",
        "avg_task_seconds",
        "max_task_seconds",
    ]

    rows = []
    for (dname, dgroup), recs in sorted(groups.items()):
        total = len(recs)
        success = sum(1 for r in recs if r.execution_status == "EXEC_SUCCESS")
        failed = total - success
        started = min(r.started_at for r in recs) if recs else 0
        ended = max(r.ended_at for r in recs) if recs else 0
        wall_clock = max(ended - started, 0)
        sum_dur = sum(r.duration_seconds for r in recs)
        avg_dur = sum_dur / total if total else 0
        max_dur = max(r.duration_seconds for r in recs) if recs else 0

        rows.append([
            dname, dgroup,
            str(total), str(success), str(failed),
            str(round(wall_clock, 1)),
            str(round(sum_dur, 1)),
            str(round(avg_dur, 1)),
            str(round(max_dur, 1)),
        ])

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)

    logger.info("Wrote device_timing.csv to %s (%d rows)", path, len(rows))
    return path


def write_endpoint_timing_csv(
    results: Sequence[ExecutionResult],
    output_dir: str,
    filename: str = "endpoint_timing.csv",
) -> str:
    """Write per-endpoint aggregated timing to CSV."""
    if not is_safe_path_component(filename):
        raise ValueError(f"Unsafe filename for report: {filename!r}")
    path = safe_join_under_root(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)

    # Group by endpoint_key + endpoint_type
    groups: dict[tuple[str, str], list[ExecutionResult]] = defaultdict(list)
    for r in results:
        groups[(r.endpoint_key, r.endpoint_type)].append(r)

    header = [
        "endpoint_key", "endpoint_type",
        "total_tasks", "success", "failed",
        "first_started_at", "last_finished_at",
        "wall_clock_seconds",
        "sum_plan_duration_seconds",
        "sum_resource_wait_seconds",
        "avg_wait_seconds",
        "max_task_seconds",
    ]

    rows = []
    for (ekey, etype), recs in sorted(groups.items()):
        total = len(recs)
        success = sum(1 for r in recs if r.execution_status == "EXEC_SUCCESS")
        failed = total - success
        first_start = min(r.started_at for r in recs) if recs else 0
        last_end = max(r.ended_at for r in recs) if recs else 0
        wall_clock = max(last_end - first_start, 0)
        sum_dur = sum(r.duration_seconds for r in recs)
        sum_wait = sum(r.resource_wait_seconds for r in recs)
        avg_wait = sum_wait / total if total else 0
        max_dur = max(r.duration_seconds for r in recs) if recs else 0

        rows.append([
            ekey, etype,
            str(total), str(success), str(failed),
            _fmt_ts(first_start), _fmt_ts(last_end),
            str(round(wall_clock, 1)),
            str(round(sum_dur, 1)),
            str(round(sum_wait, 1)),
            str(round(avg_wait, 1)),
            str(round(max_dur, 1)),
        ])

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)

    logger.info("Wrote endpoint_timing.csv to %s (%d rows)", path, len(rows))
    return path


def write_execution_summary(
    results: Sequence[ExecutionResult],
    output_dir: str,
    execution_started_at: float | None = None,
    execution_id: str = "",
    stop_metadata: dict | None = None,
) -> str:
    """Write execution summary to JSON (and CSV)."""
    path = safe_join_under_root(output_dir, "execution_summary.json")
    os.makedirs(output_dir, exist_ok=True)

    total = len(results)
    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    failed = sum(1 for r in results if r.execution_status not in ("EXEC_SUCCESS",))
    started = execution_started_at or (min(r.started_at for r in results) if results else 0)
    ended = max(r.ended_at for r in results) if results else 0
    wall_clock = max(ended - started, 0)

    sum_plan = sum(r.duration_seconds for r in results)
    sum_exec = sum(r.executor_duration_seconds for r in results)
    sum_wait = sum(r.resource_wait_seconds for r in results)
    parallel_efficiency = round(sum_plan / wall_clock, 2) if wall_clock > 0 else 0

    # Find slowest endpoint
    ep_times: dict[str, float] = defaultdict(float)
    for r in results:
        ep_times[r.endpoint_key] += r.duration_seconds
    slowest_endpoint = max(ep_times, key=ep_times.get) if ep_times else ""

    # Find slowest task
    task_times: dict[str, float] = defaultdict(float)
    for r in results:
        task_times[r.task_name] += r.duration_seconds
    slowest_task = max(task_times, key=task_times.get) if task_times else ""

    # Find top wait endpoint
    ep_waits: dict[str, float] = defaultdict(float)
    for r in results:
        ep_waits[r.endpoint_key] += r.resource_wait_seconds
    top_wait_endpoint = max(ep_waits, key=ep_waits.get) if ep_waits else ""

    summary = {
        "execution_id": execution_id,
        "total_plans": total,
        "success_count": success,
        "failed_count": failed,
        "execution_started_at": _fmt_ts(started),
        "execution_finished_at": _fmt_ts(ended),
        "wall_clock_seconds": round(wall_clock, 1),
        "sum_plan_duration_seconds": round(sum_plan, 1),
        "sum_executor_duration_seconds": round(sum_exec, 1),
        "sum_resource_wait_seconds": round(sum_wait, 1),
        "parallel_efficiency": parallel_efficiency,
        "slowest_endpoint": slowest_endpoint,
        "slowest_task": slowest_task,
        "top_wait_endpoint": top_wait_endpoint,
    }
    stop_metadata = stop_metadata or {}
    stopped_at = float(stop_metadata.get("stoppedAt") or 0.0)
    summary.update({
        "stopReason": str(stop_metadata.get("stopReason") or ""),
        "stopTriggeredBy": str(stop_metadata.get("stopTriggeredBy") or ""),
        "stoppedAt": _fmt_ts(stopped_at),
        "affectedPendingCount": int(stop_metadata.get("affectedPendingCount") or 0),
    })

    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info("Wrote execution_summary.json to %s", path)

    # Also write CSV version
    csv_path = safe_join_under_root(output_dir, "execution_summary.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(summary.keys())
        writer.writerow(summary.values())

    return path


def write_all_timing_reports(
    results: Sequence[ExecutionResult],
    output_dir: str,
    execution_started_at: float | None = None,
    execution_id: str = "",
    stop_metadata: dict | None = None,
) -> dict[str, str]:
    """Write all timing reports and return paths dict."""
    return {
        "plan_timing": write_plan_timing_csv(results, output_dir),
        "device_timing": write_device_timing_csv(results, output_dir),
        "endpoint_timing": write_endpoint_timing_csv(results, output_dir),
        "execution_summary": write_execution_summary(
            results, output_dir, execution_started_at, execution_id, stop_metadata,
        ),
    }


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
