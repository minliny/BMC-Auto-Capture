#!/usr/bin/env python3
"""Standalone timing report generator — completely offline, no browser, no network.

Generates all 5 timing reports using fake ExecutionResults constructed from
mock plans.  No import of BMCExecutor/BrowserManager/Playwright.

Usage:
  python scripts/generate_timing_report_offline.py --output ./timing_out
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
from src.out.collector import write_result_csv, write_final_result_csv
from src.out.summary import build_pivot_csv, write_failure_csv
from src.out.timing import write_all_timing_reports


# ------------------------------------------------------------------
# Test data: 6 devices, mix BMC / SSH
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
# Fake result generator (no browser, no scheduler, pure data)
# ------------------------------------------------------------------

def generate_fake_results() -> list[ExecutionResult]:
    """Generate fake ExecutionResults simulating a full concurrent run.

    Uses timing that mimics real execution: each plan gets a small unique
    sleep, BMC plans on same IP are serial, different endpoints overlap.
    """
    from src.scheduler.plan_generator import generate_plans

    devices = _devices()
    tasks = _tasks()
    plans = generate_plans(devices, tasks)

    # Simulate wall-clock pacing:
    # - Group plans by endpoint_key
    # - Each endpoint group starts at a staggered time
    # - Plans within same endpoint are serial
    base_time = time.time()
    results = []

    # Track per-endpoint offsets
    endpoint_start: dict[str, float] = {}
    for plan in plans:
        ek = plan.endpoint_key
        if ek not in endpoint_start:
            endpoint_start[ek] = base_time + len(endpoint_start) * 0.01
        # Each plan within the same endpoint starts slightly after the previous
        offset = endpoint_start[ek]
        plan_dur = 0.1 + (hash(plan.plan_id) % 10) * 0.01  # jitter 0.1-0.2s

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
            started_at=offset,
            ended_at=offset + plan_dur,
            duration_seconds=round(plan_dur, 3),
            endpoint_key=ek,
            endpoint_type=plan.endpoint_type,
            resource_wait_seconds=round(offset - base_time, 3),
            executor_duration_seconds=round(plan_dur, 3),
            retry_count=0,
            output_dir=os.path.join(plan.device.device_name, plan.task.task_name),
        )
        results.append(r)
        endpoint_start[ek] = offset + plan_dur

    return results


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate timing reports with fake data (offline, no browser)"
    )
    parser.add_argument("--output", "-o", default="./timing_out",
                        help="Output directory (default: ./timing_out)")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    exec_start = time.time()
    results = generate_fake_results()
    wall_clock = time.time() - exec_start

    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    print(f"Generated {len(results)} fake results ({success} success) in {wall_clock:.1f}s")
    print(f"Output: {out_dir.resolve()}")

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
    checks = ok = 0
    for name, path in sorted(paths.items()):
        assert os.path.exists(path), f"Missing: {path}"
        ok += 1
        print(f"  OK  {name}")

    rp = os.path.join(out_dir, "result.csv")
    assert os.path.exists(rp), "result.csv missing"
    with open(rp, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(results)
    print(f"  OK  result.csv: {len(rows)} rows")

    pp = paths["plan_timing"]
    with open(pp, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(results)
    for r in rows:
        assert float(r["duration_seconds"]) > 0, f"Zero duration: {r['plan_id']}"
    print(f"  OK  plan_timing.csv: {len(rows)} rows, all durations > 0")

    print(f"\n  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
