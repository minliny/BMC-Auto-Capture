"""Simplest possible test - trace manually."""
from __future__ import annotations
import sys, time, threading
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.plan_generator import generate_plans

# 2 devices × 2 tasks each = 4 plans (one BMC, one SSH per device)
devices = [
    Device(0, "D0", "G1", "10.0.0.1", "a", "p", "10.0.0.101", "u", "p", True, ()),
    Device(1, "D1", "G1", "10.0.1.1", "a", "p", "10.0.1.101", "u", "p", True, ()),
]
tasks = [
    Task(0, 0, "BMC_T0", "BMC", "BMC_URL", "", (), "/test", timeout_seconds=5, enabled=True),
    Task(1, 1, "SSH_T0", "SSH", "SSH_CMD", "", (), "show ver", timeout_seconds=5, enabled=True),
]
plans = generate_plans(devices, tasks)
print(f"Plans: {len(plans)}")

class TestScheduler(DynamicScheduler):
    def _execute_plan(self, plan):
        print(f"    EXEC {plan.device.device_name} {plan.task.task_name}", flush=True)
        time.sleep(0.01)
        r = ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
            started_at=time.time(), ended_at=time.time(),
        )
        print(f"    DONE_EXEC {plan.device.device_name} {plan.task.task_name}", flush=True)
        return r

config = AppConfig()
config.base_bmc_workers = 1
config.max_bmc_workers = 1
config.base_ssh_workers = 1
config.max_ssh_workers = 1
config.output_root = "/tmp/bmc_test"

s = TestScheduler(config)
print("Start...", flush=True)
results = s.run(plans)

print(f"Results: {len(results)}/{len(plans)}", flush=True)
remaining = sum(len(q) for q in s._device_queues.values())
print(f"Remaining: {remaining}", flush=True)
print("PASS" if len(results) == len(plans) and remaining == 0 else "FAIL")
