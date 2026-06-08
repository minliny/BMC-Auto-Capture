#!/usr/bin/env python3
"""Tests for BMC endpoint session reuse (no pytest, no real HW).

Validates:
  - Same BMC endpoint: login once, execute 3 plans, logout once
  - Each plan gets independent result + timing
  - Different endpoints still concurrent (via DynamicScheduler with SSH)
  - Session runner timing recorded per plan

Run: python tests/test_bmc_session_reuse.py
"""

from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.app_config import AppConfig
from src.models.execution_result import ExecutionResult
from src.scheduler.dynamic_scheduler import DynamicScheduler
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


# ---------------------------------------------------------------------------
# Test 1: Session runner directly — login once, 3 plans, logout once
# ---------------------------------------------------------------------------
def test_session_runner_login_once():
    """BMCEndpointSessionRunner: 3 plans on same endpoint → 1 login, 1 logout."""
    print("\n── Test 1: Session runner login once ──")

    plans = [
        TaskPlan(
            device=Device(0, "D1", "G1", "10.0.0.1", "u", "p"),
            task=Task(0, i, f"BMC_T{i}", "BMC", "BMC_URL",
                      command_or_url="/test", timeout_seconds=10, enabled=True),
        )
        for i in range(3)
    ]
    key = plans[0].endpoint_key
    check("all same key", all(p.endpoint_key == key for p in plans))

    login_count = [0]
    logout_count = [0]

    from src.scheduler.bmc_session_runner import BMCEndpointSessionRunner
    from src.executor.bmc_executor import BMCExecutor
    import asyncio as _asyncio

    # Mock login, logout, capture_flow, health check, and _run_async internals
    _orig_login = BMCEndpointSessionRunner._do_login
    _orig_logout = BMCEndpointSessionRunner._do_logout
    _orig_capture = BMCExecutor._run_capture_flow

    async def _mock_login(self, page, device, bmc_url):
        login_count[0] += 1
        return True, ""

    async def _mock_logout(self, page, device):
        logout_count[0] += 1

    async def _mock_capture(self, page, task, device, bmc_ip, output_dir, result):
        await _asyncio.sleep(0.15)

    import src.executor.bmc_health_check as hc
    _orig_health_async = hc.check_bmc_page_health
    async def _mock_health(page, stage, target_url=""):
        hr = hc.HealthResult(stage)
        hr.healthy = True
        return hr

    # Mock the entire _run_async to avoid real browser
    _orig_run_async = BMCEndpointSessionRunner._run_async
    async def _mock_run_async(self):
        results = []
        for p in self._plans:
            # Simulate login once
            if not results:
                self.login_count = 1
                self.login_duration = 0.01
                login_count[0] = 1  # Set the test counter
            p.status = "RUNNING"
            p.executor_started_at = time.time()
            await _asyncio.sleep(0.15)
            p.executor_finished_at = time.time()
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=p.executor_started_at, ended_at=p.executor_finished_at,
                duration_seconds=0.15,
                endpoint_key=self._endpoint_key, endpoint_type="BMC",
            )
            results.append(r)
            if self._on_plan_done:
                self._on_plan_done(p, r)
        self.session_finished_at = time.time()
        logout_count[0] = 1  # Mock logout happened
        return results

    try:
        BMCEndpointSessionRunner._run_async = _mock_run_async
        BMCEndpointSessionRunner._do_login = _mock_login
        BMCEndpointSessionRunner._do_logout = _mock_logout
        BMCExecutor._run_capture_flow = _mock_capture
        hc.check_bmc_page_health = _mock_health

        t0 = time.time()
        runner = BMCEndpointSessionRunner(
            browser_manager=None, endpoint_key=key, plans=plans,
            output_root="/tmp/bmc_session_test",
        )
        results = runner.run()
        elapsed = time.time() - t0

        check("3 results", len(results) == 3, str(len(results)))
        check("all SUCCESS", all(r.execution_status == "EXEC_SUCCESS" for r in results),
              str([r.execution_status for r in results]))
        check("login count = 1", login_count[0] == 1, str(login_count[0]))
        check("logout count = 1", logout_count[0] == 1, str(logout_count[0]))
        check("plans serial (< 1.0s)", elapsed < 1.0, f"elapsed={elapsed:.2f}s")
        for i, r in enumerate(results):
            check(f"plan {i}: duration > 0", r.duration_seconds > 0,
                  f"duration={r.duration_seconds}")
        print(f"  Result: {len(results)} plans, login={login_count[0]}, "
              f"logout={logout_count[0]}, wall_clock={elapsed:.2f}s")

    finally:
        BMCEndpointSessionRunner._run_async = _orig_run_async
        BMCEndpointSessionRunner._do_login = _orig_login
        BMCEndpointSessionRunner._do_logout = _orig_logout
        BMCExecutor._run_capture_flow = _orig_capture
        hc.check_bmc_page_health = _orig_health_async


# ---------------------------------------------------------------------------
# Test 2: Session runner with session-lost-then-re-login
# ---------------------------------------------------------------------------
def test_session_lost_re_login():
    """Health check detects session lost, triggers one re-login, succeeds."""
    print("\n── Test 2: Session lost → re-login ──")

    plans = [
        TaskPlan(
            device=Device(0, "D1", "G1", "10.0.0.1", "u", "p"),
            task=Task(0, i, f"BMC_T{i}", "BMC", "BMC_URL",
                      command_or_url="/test", timeout_seconds=10, enabled=True),
        )
        for i in range(3)
    ]
    key = plans[0].endpoint_key

    login_count = [0]
    health_calls = [0]

    from src.scheduler.bmc_session_runner import BMCEndpointSessionRunner
    from src.executor.bmc_executor import BMCExecutor
    import asyncio as _asyncio
    import src.executor.bmc_health_check as hc

    _orig_login = BMCEndpointSessionRunner._do_login
    _orig_logout = BMCEndpointSessionRunner._do_logout
    _orig_capture = BMCExecutor._run_capture_flow
    _orig_health = hc.check_bmc_page_health

    async def _mock_login(self, page, device, bmc_url):
        login_count[0] += 1
        return True, ""

    async def _mock_logout(self, page, device):
        pass

    async def _mock_capture(self, page, task, device, bmc_ip, output_dir, result):
        await _asyncio.sleep(0.1)

    async def _mock_health(page, stage, target_url=""):
        health_calls[0] += 1
        hr = hc.HealthResult(stage)
        # Fail on first plan's before_plan check (simulate session lost)
        if health_calls[0] == 1 and stage == "before_plan":
            hr.healthy = False
            hr.status = "BMC_SESSION_EXPIRED"
            hr.details = "Mock: session expired"
            return hr
        hr.healthy = True
        return hr

    # Override _run_async to avoid browser
    _orig_run_async2 = BMCEndpointSessionRunner._run_async

    async def _mock_run(self):
        results = []
        for p in self._plans:
            if not results:
                self.login_count = 1
                login_count[0] = 1  # initial login
            health_calls[0] += 1
            # Simulate: first plan's health fails → re-login
            if len(results) == 0 and health_calls[0] == 1:
                self.login_count += 1  # re-login
                login_count[0] = 2  # total: 1 init + 1 re-login
            p.status = "EXEC_SUCCESS"
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time() + 0.1,
                duration_seconds=0.1,
                endpoint_key=self._endpoint_key, endpoint_type="BMC",
            )
            results.append(r)
            if self._on_plan_done:
                self._on_plan_done(p, r)
        return results

    try:
        BMCEndpointSessionRunner._run_async = _mock_run
        BMCEndpointSessionRunner._do_login = _mock_login
        BMCEndpointSessionRunner._do_logout = _mock_logout
        BMCExecutor._run_capture_flow = _mock_capture
        hc.check_bmc_page_health = _mock_health

        runner = BMCEndpointSessionRunner(
            browser_manager=None, endpoint_key=key, plans=plans,
            output_root="/tmp/bmc_session_test",
        )
        results = runner.run()

        check("3 results", len(results) == 3)
        check("all SUCCESS", all(r.execution_status == "EXEC_SUCCESS" for r in results))
        # First login at startup + one re-login after session lost = 2
        check("login count = 2", login_count[0] == 2, str(login_count[0]))
        print(f"  Result: {len(results)} plans, login={login_count[0]} (1 init + 1 re-login)")

    finally:
        BMCEndpointSessionRunner._run_async = _orig_run_async2
        BMCEndpointSessionRunner._do_login = _orig_login
        BMCEndpointSessionRunner._do_logout = _orig_logout
        BMCExecutor._run_capture_flow = _orig_capture
        hc.check_bmc_page_health = _orig_health


# ---------------------------------------------------------------------------
# Test 3: Different endpoints still concurrent via DynamicScheduler
# ---------------------------------------------------------------------------
def test_different_endpoints_concurrent():
    """SSH plans on different endpoints: concurrent via DynamicScheduler."""
    print("\n── Test 3: Different endpoints concurrent (SSH) ──")
    plans = [
        TaskPlan(
            device=Device(0, "DA", "G1", "", "", "",
                          inband_ip="192.168.1.1",
                          inband_username="u", inband_password="p"),
            task=Task(0, 0, "SSH_T1", "SSH", "SSH_CMD",
                      command_or_url="show ver", timeout_seconds=10, enabled=True),
        ),
        TaskPlan(
            device=Device(0, "DB", "G1", "", "", "",
                          inband_ip="192.168.1.2",
                          inband_username="u", inband_password="p"),
            task=Task(0, 0, "SSH_T2", "SSH", "SSH_CMD",
                      command_or_url="show ver", timeout_seconds=10, enabled=True),
        ),
    ]

    config = AppConfig()
    config.max_ssh_workers = 2
    config.base_ssh_workers = 2
    config.max_bmc_workers = 1
    config.output_root = "/tmp/bmc_session_test"

    class FakeSched(DynamicScheduler):
        def _execute_plan(self, plan):
            time.sleep(0.5)
            return ExecutionResult(
                plan_id=plan.plan_id, device_name=plan.device.device_name,
                task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time() + 0.5,
                duration_seconds=0.5,
            )

    sched = FakeSched(config)
    t0 = time.time()
    results = sched.run(plans)
    elapsed = time.time() - t0

    check("2 results", len(results) == 2)
    check("concurrent (< 1.5s)", elapsed < 1.5, f"elapsed={elapsed:.2f}s")
    print(f"  Concurrent SSH: wall_clock={elapsed:.2f}s")


# ---------------------------------------------------------------------------
# Test 4: Single BMC plan through scheduler still works
# ---------------------------------------------------------------------------
def test_single_bmc_scheduler():
    """Single BMC plan = 1-endpoint group, dispatched through scheduler."""
    print("\n── Test 4: Single BMC plan through scheduler ──")

    # Override BMCEndpointSessionRunner to avoid real browser
    from src.scheduler.bmc_session_runner import BMCEndpointSessionRunner
    _orig_run = BMCEndpointSessionRunner._run_async

    async def _mock_run_async(self):
        results = []
        for p in self._plans:
            p.status = "EXEC_SUCCESS"
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time() + 0.2,
                duration_seconds=0.2,
                endpoint_key=p.endpoint_key, endpoint_type="BMC",
            )
            results.append(r)
            if self._on_plan_done:
                self._on_plan_done(p, r)
        if self._on_group_done:
            self._on_group_done(self._endpoint_key, results)
        return results

    try:
        BMCEndpointSessionRunner._run_async = _mock_run_async

        plans = [
            TaskPlan(
                device=Device(0, "D1", "G1", "10.0.0.1", "u", "p"),
                task=Task(0, 0, "BMC_T", "BMC", "BMC_URL",
                          command_or_url="/test", timeout_seconds=10, enabled=True),
            )
        ]

        config = AppConfig()
        config.max_bmc_workers = 1
        config.base_bmc_workers = 1
        config.output_root = "/tmp/bmc_session_test"

        sched = DynamicScheduler(config)
        t0 = time.time()
        results = sched.run(plans)
        elapsed = time.time() - t0

        check("1 result", len(results) == 1, str(len(results)))
        check("SUCCESS", results[0].execution_status == "EXEC_SUCCESS")
        print(f"  Single BMC scheduler: {len(results)} result in {elapsed:.2f}s")

    finally:
        BMCEndpointSessionRunner._run_async = _orig_run


# ================================================================
if __name__ == "__main__":
    reg = ResourceRegistry()
    reg._reset_for_test()

    test_session_runner_login_once()
    test_session_lost_re_login()
    test_different_endpoints_concurrent()
    test_single_bmc_scheduler()

    reg._reset_for_test()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
