"""Tests for API run_with_plans with endpoint-aware dynamic scheduler.

Run: python -m pytest tests/test_api_run_with_plans.py -v
"""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.app_config import AppConfig
from src.models.execution_result import ExecutionResult
from src.scheduler.resource_registry import ResourceRegistry


def make_device(name: str, group: str = "G1", bmc_ip: str = "", inband_ip: str = "") -> Device:
    return Device(
        row_index=0, device_name=name, device_group=group,
        bmc_ip=bmc_ip, bmc_username="u", bmc_password="p",
        inband_ip=inband_ip, inband_username="u", inband_password="p",
        enabled=True,
    )


def make_bmc_task(name: str) -> Task:
    return Task(
        row_index=0, sequence=0, task_name=name, task_type="BMC",
        execution_mode="BMC_URL", command_or_url="/test",
        timeout_seconds=10, enabled=True,
    )


def make_inband_task(name: str, task_type: str = "SSH") -> Task:
    return Task(
        row_index=0, sequence=0, task_name=name, task_type=task_type,
        execution_mode="SSH_CMD" if task_type == "SSH" else "TELNET_CMD",
        command_or_url="show ver", timeout_seconds=10, enabled=True,
    )


@pytest.fixture(autouse=True)
def reset_registry():
    reg = ResourceRegistry()
    reg._reset_for_test()
    yield
    reg._reset_for_test()


def _make_fake_dynamic_app(config, plans, results_list):
    """Helper: run_with_plans(mode='full') with fake executor."""
    from src.app import App
    app = App(config)
    def mock_dynamic(plist):
        from src.scheduler.dynamic_scheduler import DynamicScheduler
        class FakeSched(DynamicScheduler):
            def _execute_plan(self, plan):
                time.sleep(0.5)
                plan.executor_started_at = time.time()
                plan.executor_finished_at = time.time() + 0.5
                return ExecutionResult(
                    plan_id=plan.plan_id, device_name=plan.device.device_name,
                    task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
                    started_at=time.time(), ended_at=time.time() + 0.5,
                    duration_seconds=0.5,
                )
        sched = FakeSched(config)
        res = sched.run(plist)
        app._results.extend(res)
        return res
    app._execute_dynamic = mock_dynamic
    results_list.extend(app.run_with_plans(plans, mode="full"))


# ---------------------------------------------------------------------------
# Test: Two executions, same endpoint → serial via global registry
# ---------------------------------------------------------------------------
def test_two_executions_same_endpoint_serial():
    """Two run_with_plans calls on same endpoint → registry enforces serial."""
    plans_a = [TaskPlan(device=make_device("DA", inband_ip="10.0.0.1"),
                        task=make_inband_task("SSH_T1"))]
    plans_b = [TaskPlan(device=make_device("DB", inband_ip="10.0.0.1"),  # Same IP!
                        task=make_inband_task("SSH_T1"))]

    config = AppConfig()
    config.max_bmc_workers = 2
    config.base_bmc_workers = 2
    config.output_root = "/tmp/bmc_test_api"
    config.preflight_enabled = False
    config.route_guard_enabled = False

    results_a = []
    results_b = []

    t0 = time.time()
    t1 = threading.Thread(target=_make_fake_dynamic_app, args=(config, plans_a, results_a))
    t2 = threading.Thread(target=_make_fake_dynamic_app, args=(config, plans_b, results_b))
    t1.start()
    time.sleep(0.05)  # Ensure t1 acquires first
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    elapsed = time.time() - t0

    assert len(results_a) == 1, f"Expected 1 result in A, got {len(results_a)}"
    assert len(results_b) == 1, f"Expected 1 result in B, got {len(results_b)}"
    # Same endpoint must be serial → >= 0.9s
    assert elapsed >= 0.8, f"Two executions not serialized: {elapsed:.2f}s (expected >=0.8s)"
    print(f"  PASS: two executions same endpoint serial — wall_clock={elapsed:.2f}s")


# ---------------------------------------------------------------------------
# Test: Two executions, different endpoints → concurrent
# ---------------------------------------------------------------------------
def test_two_executions_different_endpoint_concurrent():
    """Two run_with_plans calls on different endpoints → concurrent."""
    plans_a = [TaskPlan(device=make_device("DA", inband_ip="10.0.0.1"),
                        task=make_inband_task("SSH_T1"))]
    plans_b = [TaskPlan(device=make_device("DB", inband_ip="10.0.0.2"),  # Different IP
                        task=make_inband_task("SSH_T1"))]

    config = AppConfig()
    config.max_bmc_workers = 2
    config.base_bmc_workers = 2
    config.output_root = "/tmp/bmc_test_api"
    config.preflight_enabled = False
    config.route_guard_enabled = False

    results_a = []
    results_b = []

    t0 = time.time()
    t1 = threading.Thread(target=_make_fake_dynamic_app, args=(config, plans_a, results_a))
    t2 = threading.Thread(target=_make_fake_dynamic_app, args=(config, plans_b, results_b))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    elapsed = time.time() - t0

    assert len(results_a) == 1
    assert len(results_b) == 1
    # Different endpoints → concurrent → < 1.5s
    assert elapsed < 1.5, f"Different endpoints not concurrent: {elapsed:.2f}s"
    print(f"  PASS: two executions different endpoints concurrent — wall_clock={elapsed:.2f}s")


# ---------------------------------------------------------------------------
# Test: run_with_plans sequential mode still works
# ---------------------------------------------------------------------------
def test_run_with_plans_sequential_mode():
    """run_with_plans mode='sequential' still works (backward compat)."""
    plans = [
        TaskPlan(device=make_device("D1", bmc_ip="10.0.0.1"), task=make_bmc_task("T1")),
        TaskPlan(device=make_device("D2", bmc_ip="10.0.0.2"), task=make_bmc_task("T2")),
    ]

    config = AppConfig()
    config.output_root = "/tmp/bmc_test_api_seq"
    config.preflight_enabled = False
    config.route_guard_enabled = False

    from src.app import App
    app = App(config)

    # Patch _execute_sequential to use fake executor
    original_seq = app._execute_sequential
    def fake_seq(plans):
        for p in plans:
            p.status = "SUCCESS"
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time(), duration_seconds=0.1,
            )
            app._results.append(r)

    app._execute_sequential = fake_seq

    t0 = time.time()
    results = app.run_with_plans(plans, mode="sequential")
    elapsed = time.time() - t0

    assert len(results) == 2
    print(f"  PASS: run_with_plans sequential mode — {len(results)} results in {elapsed:.2f}s")


# ===================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
