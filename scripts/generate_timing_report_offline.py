#!/usr/bin/env python3
"""Standalone timing report generator — no pytest, no HW.

Simulates a full execution with fake executors and writes all 5 timing
reports to an output directory.  Useful for verifying the timing pipeline
on Windows without real BMC/SSH devices.

Usage:
  python scripts/generate_timing_report_offline.py [--output ./timing_out]

Output files:
  result.csv
  plan_timing.csv
  device_timing.csv
  endpoint_timing.csv
  execution_summary.csv
  execution_summary.json
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.out.collector import write_result_csv, write_final_result_csv
from src.out.summary import build_pivot_csv, write_failure_csv
from src.out.timing import write_all_timing_reports


# ------------------------------------------------------------------
# Test data: 6 devices, mix of BMC / SSH / TELNET
# ------------------------------------------------------------------

def _devices():
    return [
        Device(0, "A3-01", "A3", "10.0.1.1", "admin", "pass",
               inband_ip="192.168.1.1", inband_username="root", inband_password="p"),
        Device(1, "A3-02", "A3", "10.0.1.2", "admin", "pass",
               inband_ip="192.168.1.2", inband_username="root", inband_password="p"),
        Device(2, "L1-01", "L1", "", "", "",
               inband_ip="10.0.2.1", inband_username="admin", inband_password="p"),
        Device(3, "L2-01", "L2", "10.0.3.1", "admin", "pass",
               inband_ip="", inband_username="", inband_password=""),
        Device(4, "RM211-01", "RM211", "10.0.4.1", "admin", "pass",
               inband_ip="192.168.4.1", inband_username="root", inband_password="p"),
        Device(5, "RM211-02", "RM211", "10.0.4.1", "admin", "pass",
               inband_ip="192.168.4.2", inband_username="root", inband_password="p"),
    ]


def _tasks():
    return [
        Task(0, 1, "BMC首页截图", "BMC", "BMC_URL",
             command_or_url="/UI/Static/#/navigate/home", timeout_seconds=30, enabled=True),
        Task(1, 2, "BMC系统信息", "BMC", "BMC_URL",
             command_or_url="/UI/Static/#/navigate/system/info", timeout_seconds=30, enabled=True),
        Task(2, 3, "SSH系统状态", "SSH", "SSH_CMD",
             command_or_url="show system status", timeout_seconds=30, enabled=True),
        Task(3, 4, "SSH端口查询", "SSH", "SSH_CMD",
             command_or_url="show port status", timeout_seconds=30, enabled=True),
        Task(4, 5, "TELNET巡检", "TELNET", "TELNET_CMD",
             command_or_url="display version", timeout_seconds=30, enabled=True),
    ]


# ------------------------------------------------------------------
# Fake scheduler
# ------------------------------------------------------------------

class TimingScheduler(DynamicScheduler):
    """Uses time.sleep() as fake executor to simulate real timing."""

    def _execute_plan(self, plan):
        plan.executor_started_at = time.time()
        time.sleep(0.15)  # Simulate realistic execution time
        plan.executor_finished_at = time.time()
        r = ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            device_group=plan.device.device_group,
            bmc_ip=plan.device.bmc_ip,
            inband_ip=plan.device.inband_ip,
            task_name=plan.task.task_name,
            task_type=plan.task.task_type,
            execution_mode=plan.task.execution_mode,
            execution_status="EXEC_SUCCESS",
            started_at=plan.executor_started_at,
            ended_at=plan.executor_finished_at,
            duration_seconds=round(plan.executor_finished_at - plan.executor_started_at, 3),
            endpoint_key=plan.endpoint_key,
            endpoint_type=plan.endpoint_type,
            resource_wait_seconds=plan.resource_wait_seconds,
            executor_duration_seconds=plan.executor_duration_seconds,
            retry_count=plan.retry_attempt,
            output_dir=os.path.join(plan.device.device_name, plan.task.task_name),
        )
        plan.ended_at = plan.executor_finished_at
        return r


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate timing reports with fake executors (offline verification)"
    )
    parser.add_argument("--output", "-o", default="./timing_out",
                        help="Output directory (default: ./timing_out)")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    from src.scheduler.plan_generator import generate_plans

    devices = _devices()
    tasks = _tasks()
    plans = generate_plans(devices, tasks)

    print(f"Devices: {len(devices)}")
    print(f"Tasks:   {len(tasks)}")
    print(f"Plans:   {len(plans)}")
    print(f"Output:  {out_dir.resolve()}")
    print()

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.max_ssh_workers = 4
    config.base_ssh_workers = 4
    config.output_root = str(out_dir)

    exec_start = time.time()
    scheduler = TimingScheduler(config)
    results = scheduler.run(plans)
    wall_clock = time.time() - exec_start

    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    failed = len(results) - success
    print(f"\nResults: {len(results)} total, {success} success, {failed} failed")
    print(f"Wall clock: {wall_clock:.1f}s")

    # Write standard reports
    write_result_csv(results, str(out_dir))
    write_final_result_csv(results, str(out_dir))
    build_pivot_csv(results, str(out_dir))
    write_failure_csv(results, str(out_dir))

    # Write timing reports
    paths = write_all_timing_reports(results, str(out_dir),
                                     execution_started_at=exec_start)

    print(f"\nGenerated files:")
    for name, path in sorted(paths.items()):
        size = os.path.getsize(path) if os.path.exists(path) else 0
        print(f"  {name:20s}  {size:>6d} B  {path}")

    # Quick validation
    print(f"\n── Validation ──")
    checks = 0
    fails = 0

    # result.csv
    rp = os.path.join(out_dir, "result.csv")
    with open(rp, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(results), f"result.csv: expected {len(results)} rows, got {len(rows)}"
    print(f"  OK  result.csv: {len(rows)} rows")

    # plan_timing.csv
    pp = paths["plan_timing"]
    with open(pp, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(results)
    for r in rows:
        assert float(r["duration_seconds"]) > 0, f"plan_timing.csv: zero duration for {r['plan_id']}"
    print(f"  OK  plan_timing.csv: {len(rows)} rows, all durations > 0")

    # device_timing.csv
    with open(paths["device_timing"], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 4, f"device_timing.csv: expected >=4 devices, got {len(rows)}"
    print(f"  OK  device_timing.csv: {len(rows)} rows")

    # endpoint_timing.csv
    with open(paths["endpoint_timing"], encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 3, f"endpoint_timing.csv: expected >=3 endpoints, got {len(rows)}"
    print(f"  OK  endpoint_timing.csv: {len(rows)} rows")

    # execution_summary.json
    with open(paths["execution_summary"], encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["total_plans"] == len(results)
    assert summary["parallel_efficiency"] > 0
    assert "slowest_endpoint" in summary
    assert "slowest_task" in summary
    print(f"  OK  execution_summary.json: parallel_efficiency={summary['parallel_efficiency']:.2f}")

    print(f"\n  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
