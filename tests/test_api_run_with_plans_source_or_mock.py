#!/usr/bin/env python3
"""Standalone API run_with_plans source-import tests (no pytest, no server).

Validates by directly importing App and mocking executors:
  - mode='full' uses endpoint-aware scheduler
  - mode='sequential' backward compat
  - two concurrent executions same endpoint → serial
  - two concurrent executions different endpoint → concurrent

Run: python tests/test_api_run_with_plans_source_or_mock.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.app_config import AppConfig
from src.models.execution_result import ExecutionResult
from src.scheduler.resource_registry import ResourceRegistry

FAILS = 0
TOTAL = 0


def check(name: str, cond: bool, detail: str = ""):
    global FAILS, TOTAL
    TOTAL += 1
    if cond:
        print(f"  OK  {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------------------------
# Shared fake-dynamic helper: patches App._execute_dynamic with a
# FakeScheduler that uses time.sleep() as the executor.
# ------------------------------------------------------------------

def _make_fake_dynamic_app(config, plans, results_list):
    """Run app.run_with_plans(mode='full') with fake executor.

    The fake executor sleeps 0.5s per plan, which allows us to measure
    wall-clock and verify serial vs concurrent scheduling.
    """
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
                    plan_id=plan.plan_id,
                    device_name=plan.device.device_name,
                    task_name=plan.task.task_name,
                    execution_status="EXEC_SUCCESS",
                    started_at=time.time(),
                    ended_at=time.time() + 0.5,
                    duration_seconds=0.5,
                )
        sched = FakeSched(config)
        res = sched.run(plist)
        app._results.extend(res)
        return res

    app._execute_dynamic = mock_dynamic
    results_list.extend(app.run_with_plans(plans, mode="full"))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _bmc_plan(name: str, ip: str) -> TaskPlan:
    d = Device(0, name, "G1", bmc_ip=ip, bmc_username="u", bmc_password="p")
    t = Task(0, 0, "BMC_T", "BMC", "BMC_URL", command_or_url="/test",
             timeout_seconds=10, enabled=True)
    return TaskPlan(device=d, task=t)


def _prep_config() -> AppConfig:
    c = AppConfig()
    c.max_bmc_workers = 2
    c.base_bmc_workers = 2
    c.max_ssh_workers = 2
    c.base_ssh_workers = 2
    c.output_root = "/tmp/bmc_test_api_offline"
    c.preflight_enabled = False
    c.route_guard_enabled = False
    return c


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_full_mode_basic():
    """run_with_plans mode='full' returns results."""
    print("\n── full mode basic ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    plans = [_bmc_plan("D1", "10.0.0.1")]
    results = []
    _make_fake_dynamic_app(_prep_config(), plans, results)
    check("returns 1 result", len(results) == 1, str(len(results)))
    check("status SUCCESS", results[0].execution_status == "EXEC_SUCCESS")
    reg._reset_for_test()


def test_two_executions_same_endpoint_serial():
    """Two concurrent run_with_plans with same BMC IP → serial."""
    print("\n── two executions same endpoint serial ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    config = _prep_config()
    results_a = []
    results_b = []

    t0 = time.time()
    t1 = threading.Thread(target=_make_fake_dynamic_app,
                          args=(config, [_bmc_plan("DA", "10.0.0.1")], results_a))
    t2 = threading.Thread(target=_make_fake_dynamic_app,
                          args=(config, [_bmc_plan("DB", "10.0.0.1")], results_b))
    t1.start()
    time.sleep(0.05)  # Ensure t1 acquires first
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    elapsed = time.time() - t0

    check("both got 1 result", len(results_a) == 1 and len(results_b) == 1,
          f"A={len(results_a)} B={len(results_b)}")
    check("serial (>=0.8s)", elapsed >= 0.8, f"elapsed={elapsed:.2f}s")
    reg._reset_for_test()


def test_two_executions_different_endpoint_concurrent():
    """Two concurrent run_with_plans with different BMC IPs → concurrent."""
    print("\n── two executions different endpoint concurrent ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    config = _prep_config()
    results_a = []
    results_b = []

    t0 = time.time()
    t1 = threading.Thread(target=_make_fake_dynamic_app,
                          args=(config, [_bmc_plan("DA", "10.0.0.1")], results_a))
    t2 = threading.Thread(target=_make_fake_dynamic_app,
                          args=(config, [_bmc_plan("DB", "10.0.0.2")], results_b))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    elapsed = time.time() - t0

    check("both got 1 result", len(results_a) == 1 and len(results_b) == 1)
    check("concurrent (<1.5s)", elapsed < 1.5, f"elapsed={elapsed:.2f}s")
    reg._reset_for_test()


def test_sequential_mode():
    """run_with_plans mode='sequential' still works."""
    print("\n── sequential mode backward compat ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    from src.app import App
    config = _prep_config()
    app = App(config)

    # Patch _execute_sequential
    def fake_seq(plans):
        for p in plans:
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time(), duration_seconds=0.1,
            )
            app._results.append(r)
    app._execute_sequential = fake_seq

    plans = [
        _bmc_plan("D1", "10.0.0.1"),
        _bmc_plan("D2", "10.0.0.2"),
    ]
    results = app.run_with_plans(plans, mode="sequential")

    check("returns 2 results", len(results) == 2, str(len(results)))
    check("both SUCCESS", all(r.execution_status == "EXEC_SUCCESS" for r in results))
    reg._reset_for_test()


# ================================================================
if __name__ == "__main__":
    test_full_mode_basic()
    test_two_executions_same_endpoint_serial()
    test_two_executions_different_endpoint_concurrent()
    test_sequential_mode()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
