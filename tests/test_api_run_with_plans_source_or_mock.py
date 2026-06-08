#!/usr/bin/env python3
"""Standalone API run_with_plans tests with fake executors (no real browser).

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
# Module-level session runner mock
# ------------------------------------------------------------------

def _install_fake_session_runner(sleep_s: float = 0.1):
    """Replace BMCEndpointSessionRunner with fake at import-time."""
    import src.scheduler.bmc_session_runner as bsr
    from tests.fakes import FakeBMCSessionRunner

    class _Fake(FakeBMCSessionRunner):
        pass
    _Fake.sleep_seconds = sleep_s

    _orig = bsr.BMCEndpointSessionRunner
    bsr.BMCEndpointSessionRunner = lambda **kw: _Fake(
        browser_manager=None, endpoint_key=kw.get("endpoint_key", ""),
        plans=kw.get("plans", []), output_root=kw.get("output_root", ""),
        on_plan_done=kw.get("on_plan_done"),
        on_group_done=kw.get("on_group_done"),
    )
    return bsr, _orig


def _restore(bsr, orig):
    bsr.BMCEndpointSessionRunner = orig


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _bmc_plan(name: str, ip: str) -> TaskPlan:
    d = Device(0, name, "G1", ip, "u", "p")
    t = Task(0, 0, f"{name}_T", "BMC", "BMC_URL", command_or_url="/test",
             timeout_seconds=10, enabled=True)
    return TaskPlan(device=d, task=t)


def _ssh_plan(name: str, ip: str) -> TaskPlan:
    d = Device(0, name, "G1", "", "", "", inband_ip=ip,
               inband_username="u", inband_password="p")
    t = Task(0, 0, f"{name}_T", "SSH", "SSH_CMD", command_or_url="show ver",
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

def test_full_mode_same_endpoint_serial():
    """Two concurrent run_with_plans, same BMC IP → serial via registry."""
    print("\n── full mode: same endpoint serial ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    bsr, orig = _install_fake_session_runner(sleep_s=0.3)

    try:
        from src.app import App
        config = _prep_config()
        results_a = []
        results_b = []

        def runner(plans, rlist):
            app = App(config)
            rlist.extend(app.run_with_plans(plans, mode="full"))

        plans_a = [_bmc_plan("DA", "10.0.0.1")]
        plans_b = [_bmc_plan("DB", "10.0.0.1")]  # Same IP

        t0 = time.time()
        t1 = threading.Thread(target=runner, args=(plans_a, results_a))
        t2 = threading.Thread(target=runner, args=(plans_b, results_b))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        elapsed = time.time() - t0

        check("both got 1 result", len(results_a) == 1 and len(results_b) == 1,
              f"A={len(results_a)} B={len(results_b)}")
        check("serial (>=0.5s)", elapsed >= 0.5, f"elapsed={elapsed:.2f}s")
    finally:
        _restore(bsr, orig)
        reg._reset_for_test()


def test_full_mode_different_endpoint_concurrent():
    """Two concurrent run_with_plans, different BMC IP → concurrent."""
    print("\n── full mode: different endpoint concurrent ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    bsr, orig = _install_fake_session_runner(sleep_s=0.3)

    try:
        from src.app import App
        config = _prep_config()
        results_a = []
        results_b = []

        def runner(plans, rlist):
            app = App(config)
            rlist.extend(app.run_with_plans(plans, mode="full"))

        plans_a = [_bmc_plan("DA", "10.0.0.1")]
        plans_b = [_bmc_plan("DB", "10.0.0.2")]  # Different IP

        t0 = time.time()
        t1 = threading.Thread(target=runner, args=(plans_a, results_a))
        t2 = threading.Thread(target=runner, args=(plans_b, results_b))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        elapsed = time.time() - t0

        check("both got 1 result", len(results_a) == 1 and len(results_b) == 1)
        check("concurrent (<1.5s)", elapsed < 1.5, f"elapsed={elapsed:.2f}s")
    finally:
        _restore(bsr, orig)
        reg._reset_for_test()


def test_full_mode_basic():
    """Single run_with_plans full mode returns results."""
    print("\n── full mode: basic ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    bsr, orig = _install_fake_session_runner(sleep_s=0.1)

    try:
        from src.app import App
        config = _prep_config()
        app = App(config)
        results = app.run_with_plans([_bmc_plan("D1", "10.0.0.1")], mode="full")
        check("returns 1 result", len(results) == 1, str(len(results)))
        check("SUCCESS", results[0].execution_status == "EXEC_SUCCESS")
    finally:
        _restore(bsr, orig)
        reg._reset_for_test()


def test_sequential_mode():
    """run_with_plans mode='sequential' still works."""
    print("\n── sequential mode ──")
    from src.app import App
    config = _prep_config()
    app = App(config)

    # sequential mode doesn't use session runner, just _execute_sequential
    def fake_seq(plans):
        for p in plans:
            r = ExecutionResult(
                plan_id=p.plan_id, device_name=p.device.device_name,
                task_name=p.task.task_name, execution_status="EXEC_SUCCESS",
                started_at=time.time(), ended_at=time.time(), duration_seconds=0.1,
            )
            app._results.append(r)
    app._execute_sequential = fake_seq

    plans = [_bmc_plan("D1", "10.0.0.1"), _bmc_plan("D2", "10.0.0.2")]
    results = app.run_with_plans(plans, mode="sequential")
    check("returns 2 results", len(results) == 2, str(len(results)))
    check("both SUCCESS", all(r.execution_status == "EXEC_SUCCESS" for r in results))


# ================================================================
if __name__ == "__main__":
    reg = ResourceRegistry()
    reg._reset_for_test()
    test_full_mode_basic()
    test_full_mode_same_endpoint_serial()
    test_full_mode_different_endpoint_concurrent()
    test_sequential_mode()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
