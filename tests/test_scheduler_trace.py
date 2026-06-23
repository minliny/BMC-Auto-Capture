"""Minimal repro: trace scheduler with fake executors."""
from __future__ import annotations
import pytest
import sys, time, logging
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
    devices = []
    for i in range(4):
        devices.append(Device(
            row_index=i, device_name=f"D{i:02d}", device_group="G1",
            bmc_ip=f"10.0.{i}.1", bmc_username="a", bmc_password="p",
            inband_ip=f"10.0.{i}.2", inband_username="u", inband_password="p",
            enabled=True, tags=(),
        ))
    tasks = []
    for j in range(3):
        tasks.append(Task(
            row_index=j, sequence=j, task_name=f"BMC_T{j}", task_type="BMC",
            execution_mode="BMC_URL", match_group="",
            command_or_url="/test", timeout_seconds=10, enabled=True,
        ))
    for j in range(3):
        tasks.append(Task(
            row_index=j+3, sequence=j+3, task_name=f"SSH_T{j}", task_type="SSH",
            execution_mode="SSH_CMD", match_group="",
            command_or_url="show ver", timeout_seconds=10, enabled=True,
        ))
    return generate_plans(devices, tasks)


class _TestScheduler(DynamicScheduler):
    def _execute_plan(self, plan):
        time.sleep(0.05)
        return ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            task_name=plan.task.task_name,
            execution_status="EXEC_SUCCESS",
            started_at=time.time(),
            ended_at=time.time(),
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

def test_scheduler_trace_clean_shutdown():
    """4 devices × 6 tasks — all must complete with clean shutdown."""
    plans = _build_plans()
    assert len(plans) >= 20, f"Expected ≥20 plans, got {len(plans)}"

    FakeScheduler, restore_token = make_fake_dynamic_scheduler(_make_config(), sleep_seconds=0.001)
    try:
        scheduler = FakeScheduler(_make_config())
        t0 = time.time()
        results = scheduler.run(plans)
        elapsed = time.time() - t0
    finally:
        restore_session_runner(restore_token)

    remaining = sum(len(q) for q in scheduler._endpoint_queues.values())
    running = len(scheduler._bmc_pool._active_futures) + len(scheduler._ssh_pool._active_futures)
    locked = len(scheduler._bmc_pool._running_resources) + len(scheduler._ssh_pool._running_resources)

    assert len(results) == len(plans), f"Missing results: {len(results)}/{len(plans)}"
    assert remaining == 0, f"Remaining queues: {remaining}"
    assert running == 0, f"Still running: {running}"
    assert locked == 0, f"Still locked: {locked}"
    print(f"PASS: {len(results)}/{len(plans)} results in {elapsed:.1f}s")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    plans = _build_plans()
    print(f"Plans: {len(plans)} (4 devices × 6 tasks = 24 max)")
    scheduler = _TestScheduler(_make_config())
    print(f"Starting scheduler with {len(plans)} plans...")
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0
    print(f"\nDone: {len(results)}/{len(plans)} results in {elapsed:.1f}s")
    remaining = sum(len(q) for q in scheduler._endpoint_queues.values())
    running = len(scheduler._bmc_pool._active_futures) + len(scheduler._ssh_pool._active_futures)
    locked = len(scheduler._bmc_pool._running_resources) + len(scheduler._ssh_pool._running_resources)
    print(f"Remaining: queues={remaining} running={running} locked={locked}")
    if len(results) == len(plans) and remaining == 0 and running == 0 and locked == 0:
        print("PASS: All plans completed, clean shutdown")
    else:
        print(f"FAIL: {len(plans) - len(results)} missing results")
