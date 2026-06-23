"""Simplest possible test - trace manually."""
from __future__ import annotations
import pytest
import sys, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.plan_generator import generate_plans
from tests.fakes import make_fake_dynamic_scheduler, restore_session_runner


# --- Helpers ---

def _build_plans():
    devices = [
        Device(0, "D0", "G1", "10.0.0.1", "a", "p", "10.0.0.101", "u", "p", True, ()),
        Device(1, "D1", "G1", "10.0.1.1", "a", "p", "10.0.1.101", "u", "p", True, ()),
    ]
    tasks = [
        Task(0, 0, "BMC_T0", "BMC", "BMC_URL", "", (), "/test", timeout_seconds=5, enabled=True),
        Task(1, 1, "SSH_T0", "SSH", "SSH_CMD", "", (), "show ver", timeout_seconds=5, enabled=True),
    ]
    return generate_plans(devices, tasks)


class _TestScheduler(DynamicScheduler):
    def _execute_plan(self, plan):
        time.sleep(0.01)
        return ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
            started_at=time.time(), ended_at=time.time(),
        )


def _make_config():
    config = AppConfig()
    config.base_bmc_workers = 1
    config.max_bmc_workers = 1
    config.base_ssh_workers = 1
    config.max_ssh_workers = 1
    config.output_root = "/tmp/bmc_test"
    config.resource_check_interval = 0.001
    config.scheduler_loop_interval = 0.001
    return config


# --- Tests ---

def test_simple_scheduler_clean_shutdown():
    """2 devices × 2 tasks = 4 plans — all must complete."""
    plans = _build_plans()
    assert len(plans) == 4, f"Expected 4 plans, got {len(plans)}"

    FakeScheduler, restore_token = make_fake_dynamic_scheduler(_make_config(), sleep_seconds=0.001)
    try:
        s = FakeScheduler(_make_config())
        results = s.run(plans)
    finally:
        restore_session_runner(restore_token)

    remaining = sum(len(q) for q in s._endpoint_queues.values())
    assert len(results) == len(plans), f"Missing results: {len(results)}/{len(plans)}"
    assert remaining == 0, f"Remaining: {remaining}"
    print(f"PASS: {len(results)}/{len(plans)} results")


if __name__ == "__main__":
    plans = _build_plans()
    print(f"Plans: {len(plans)}")
    s = _TestScheduler(_make_config())
    print("Start...", flush=True)
    results = s.run(plans)
    print(f"Results: {len(results)}/{len(plans)}", flush=True)
    remaining = sum(len(q) for q in s._endpoint_queues.values())
    print(f"Remaining: {remaining}", flush=True)
    print("PASS" if len(results) == len(plans) and remaining == 0 else "FAIL")
