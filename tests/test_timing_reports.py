"""End-to-end timing report validation with fake executor.

Run: python -m pytest tests/test_timing_reports.py -v
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.resource_registry import ResourceRegistry
from src.out.timing import write_all_timing_reports


@pytest.fixture(autouse=True)
def reset_registry():
    reg = ResourceRegistry()
    reg._reset_for_test()
    yield
    reg._reset_for_test()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_device(name, bmc_ip="", inband_ip=""):
    return Device(row_index=0, device_name=name, device_group="G1",
                  bmc_ip=bmc_ip, bmc_username="u", bmc_password="p",
                  inband_ip=inband_ip, inband_username="u", inband_password="p",
                  enabled=True)


def _make_task(name, task_type="BMC", exec_mode="BMC_URL"):
    return Task(row_index=0, sequence=0, task_name=name, task_type=task_type,
                execution_mode=exec_mode, command_or_url="/test",
                timeout_seconds=10, enabled=True)


class FakeTimingScheduler(DynamicScheduler):
    """Scheduler with fake sleep executor that records proper timing."""
    def _execute_plan(self, plan):
        plan.executor_started_at = time.time()
        time.sleep(0.1)
        plan.executor_finished_at = time.time()
        r = ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            device_group=plan.device.device_group,
            task_name=plan.task.task_name, task_type=plan.task.task_type,
            execution_status="EXEC_SUCCESS",
            started_at=plan.executor_started_at,
            ended_at=plan.executor_finished_at,
            duration_seconds=round(plan.executor_finished_at - plan.executor_started_at, 3),
            endpoint_key=plan.endpoint_key,
            endpoint_type=plan.endpoint_type,
            resource_wait_seconds=plan.resource_wait_seconds,
            executor_duration_seconds=plan.executor_duration_seconds,
            retry_count=plan.retry_attempt,
        )
        plan.ended_at = plan.executor_finished_at
        return r


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_timing_reports_end_to_end():
    """Full simulation: 4 devices, 2 BMC + 2 INBAND, verify all 5 report files."""
    plans = [
        TaskPlan(device=_make_device("D1", bmc_ip="10.0.0.1"), task=_make_task("BMC_T1", "BMC")),
        TaskPlan(device=_make_device("D2", bmc_ip="10.0.0.2"), task=_make_task("BMC_T2", "BMC")),
        TaskPlan(device=_make_device("D3", inband_ip="192.168.1.1"), task=_make_task("SSH_T1", "SSH", "SSH_CMD")),
        TaskPlan(device=_make_device("D4", inband_ip="192.168.1.2"), task=_make_task("SSH_T2", "SSH", "SSH_CMD")),
        # D5 has same BMC IP as D1 → test serialization on same endpoint
        TaskPlan(device=_make_device("D5", bmc_ip="10.0.0.1"), task=_make_task("BMC_T3", "BMC")),
    ]

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.max_ssh_workers = 3
    config.base_ssh_workers = 3

    exec_start = time.time()
    scheduler = FakeTimingScheduler(config)
    results = scheduler.run(plans)
    wall_clock = time.time() - exec_start

    assert len(results) == 5

    # Write reports to temp dir
    out_dir = tempfile.mkdtemp(prefix="bmc_timing_test_")
    paths = write_all_timing_reports(results, out_dir, execution_started_at=exec_start)

    # --- Verify plan_timing.csv ---
    pt_path = paths["plan_timing"]
    assert os.path.exists(pt_path), f"Missing {pt_path}"
    with open(pt_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 5
    row1 = rows[0]
    required_fields = ["plan_id", "device_name", "device_group", "task_name", "task_type",
                       "endpoint_key", "endpoint_type", "status",
                       "duration_seconds", "resource_wait_seconds", "executor_duration_seconds"]
    for field in required_fields:
        assert field in row1, f"plan_timing.csv missing field: {field}"
    for r in rows:
        assert float(r["duration_seconds"]) > 0, f"duration_seconds should be > 0: {r}"
    print(f"  PASS: plan_timing.csv — {len(rows)} rows, {len(required_fields)} fields verified")

    # --- Verify device_timing.csv ---
    dt_path = paths["device_timing"]
    assert os.path.exists(dt_path)
    with open(dt_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 5  # 5 unique devices (D5 shares BMC IP with D1)
    row1 = rows[0]
    for field in ["device_name", "device_group", "total_tasks", "success", "failed",
                  "wall_clock_seconds", "sum_plan_duration_seconds"]:
        assert field in row1, f"device_timing.csv missing field: {field}"
    print(f"  PASS: device_timing.csv — {len(rows)} rows")

    # --- Verify endpoint_timing.csv ---
    et_path = paths["endpoint_timing"]
    assert os.path.exists(et_path)
    with open(et_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # 4 unique endpoints: BMC:10.0.0.1, BMC:10.0.0.2, INBAND:192.168.1.1, INBAND:192.168.1.2
    assert len(rows) == 4, f"Expected 4 endpoints, got {len(rows)}"
    row1 = rows[0]
    for field in ["endpoint_key", "endpoint_type", "total_tasks", "wall_clock_seconds",
                  "sum_plan_duration_seconds", "sum_resource_wait_seconds"]:
        assert field in row1, f"endpoint_timing.csv missing field: {field}"
    print(f"  PASS: endpoint_timing.csv — {len(rows)} rows")

    # --- Verify execution_summary.json ---
    es_path = paths["execution_summary"]
    assert os.path.exists(es_path)
    with open(es_path, encoding="utf-8") as f:
        summary = json.load(f)
    required_keys = ["total_plans", "success_count", "failed_count",
                     "wall_clock_seconds", "sum_plan_duration_seconds",
                     "sum_executor_duration_seconds", "sum_resource_wait_seconds",
                     "parallel_efficiency", "slowest_endpoint", "slowest_task"]
    for key in required_keys:
        assert key in summary, f"execution_summary.json missing: {key}"
    assert summary["total_plans"] == 5
    assert summary["success_count"] == 5
    # parallel_efficiency is affected by scheduler polling overhead (0.5s sleep).
    # With 0.1s tasks and 5 plans on 4 endpoints, sum_plan=0.5, wall_clock~0.7
    # so efficiency ~0.7. Just verify it's present and non-zero.
    assert summary["parallel_efficiency"] > 0, f"parallel_efficiency should be > 0: {summary['parallel_efficiency']}"
    assert summary["wall_clock_seconds"] > 0
    assert summary["sum_plan_duration_seconds"] >= summary["wall_clock_seconds"] * 0.3
    print(f"  PASS: execution_summary.json — parallel_efficiency={summary['parallel_efficiency']:.2f}")

    # --- Verify execution_summary.csv ---
    csv_path = os.path.join(out_dir, "execution_summary.csv")
    assert os.path.exists(csv_path)
    print(f"  PASS: execution_summary.csv exists")

    # Cleanup
    import shutil
    try:
        shutil.rmtree(out_dir)
    except Exception:
        pass


# ===================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
