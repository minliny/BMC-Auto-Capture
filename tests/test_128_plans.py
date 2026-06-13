"""Test 28 devices with 128 plans (approximate user scenario)."""
from __future__ import annotations
import pytest
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.plan_generator import generate_plans


# --- Helpers: build test data, create TestScheduler ---

def _build_plans_128():
    devices = []
    for i in range(28):
        devices.append(Device(
            row_index=i, device_name=f"D{i:02d}", device_group="G1",
            bmc_ip=f"10.0.{i}.1", bmc_username="a", bmc_password="p",
            inband_ip=f"10.0.{i}.2", inband_username="u", inband_password="p",
            enabled=True,
        ))
    tasks = []
    for j in range(2):
        tasks.append(Task(j, j, f"BMC_T{j}", "BMC", "BMC_URL", "", "/test", timeout_seconds=10, enabled=True))
    for j in range(2):
        tasks.append(Task(j+2, j+2, f"SSH_T{j}", "SSH", "SSH_CMD", "", "show", timeout_seconds=10, enabled=True))
    tasks.append(Task(4, 4, "Common", "SSH", "SSH_CMD", "", "ping", timeout_seconds=10, enabled=True))
    return generate_plans(devices, tasks)


class _TestScheduler(DynamicScheduler):
    def _execute_plan(self, plan):
        time.sleep(0.02)
        return ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
            started_at=time.time(), ended_at=time.time(),
        )


def _make_config():
    config = AppConfig()
    config.base_bmc_workers = 2
    config.max_bmc_workers = 2
    config.base_ssh_workers = 4
    config.max_ssh_workers = 4
    config.output_root = "/tmp/bmc_test"
    return config


# --- Actual tests ---

def test_128_plans_complete():
    """28 devices × ~5 tasks = ~128 plans — all must complete with clean shutdown."""
    plans = _build_plans_128()
    assert len(plans) >= 120, f"Expected ~128 plans, got {len(plans)}"

    s = _TestScheduler(_make_config())
    t0 = time.time()
    results = s.run(plans)
    elapsed = time.time() - t0

    remaining = sum(len(q) for q in s._endpoint_queues.values())
    running = len(s._bmc_pool._active_futures) + len(s._ssh_pool._active_futures)

    assert len(results) == len(plans), f"Missing results: {len(results)}/{len(plans)}"
    assert remaining == 0, f"Remaining in queues: {remaining}"
    assert running == 0, f"Still running: {running}"
    print(f"PASS: {len(results)}/{len(plans)} plans in {elapsed:.1f}s")


if __name__ == "__main__":
    plans = _build_plans_128()
    print(f"Plans: {len(plans)} (28 devices × 5 tasks)")
    s = _TestScheduler(_make_config())
    print("Starting...", flush=True)
    t0 = time.time()
    results = s.run(plans)
    elapsed = time.time() - t0
    print(f"Results: {len(results)}/{len(plans)} in {elapsed:.1f}s")
    remaining = sum(len(q) for q in s._endpoint_queues.values())
    running = len(s._bmc_pool._active_futures) + len(s._ssh_pool._active_futures)
    if len(results) == len(plans) and remaining == 0 and running == 0:
        print("PASS: All plans completed, clean shutdown")
    else:
        print(f"FAIL: remaining={remaining} running={running}")
