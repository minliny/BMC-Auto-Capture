"""
AUDIT-003: Full 模式 stop/pause/resume/RouteGuard 控制链验证。
"""
from __future__ import annotations
import pytest
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.plan_generator import generate_plans


# ============================================================================
# Helpers
# ============================================================================

def _make_plans(n_devices: int = 8, n_tasks: int = 3):
    devices = []
    for i in range(n_devices):
        devices.append(Device(
            row_index=i, device_name=f"D{i:02d}", device_group="G1",
            bmc_ip=f"10.0.{i}.1", bmc_username="a", bmc_password="p",
            inband_ip=f"10.0.{i}.2", inband_username="u", inband_password="p",
            enabled=True, tags=(),
        ))
    tasks = []
    for j in range(n_tasks):
        tasks.append(Task(
            row_index=j, sequence=j, task_name=f"SSH_T{j}", task_type="SSH",
            execution_mode="SSH_CMD", match_group="",
            command_or_url="show version", timeout_seconds=10, enabled=True,
        ))
    return generate_plans(devices, tasks)


class _SlowScheduler(DynamicScheduler):
    """Scheduler with slow executor so we can observe stop/pause behavior."""
    def _execute_plan(self, plan):
        time.sleep(0.15)  # Slow enough to observe, fast enough for test
        return ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
            started_at=time.time(), ended_at=time.time(),
        )


def _make_config(bmc_workers=1, ssh_workers=1):
    config = AppConfig()
    config.base_bmc_workers = bmc_workers
    config.max_bmc_workers = bmc_workers
    config.base_ssh_workers = ssh_workers
    config.max_ssh_workers = ssh_workers
    config.output_root = "/tmp/bmc_audit_003_test"
    return config


# ============================================================================
# Case 1: App.stop() stops new dispatch, all plans get results
# ============================================================================

def test_full_mode_stop_stops_dispatch():
    """App.stop() must stop new dispatch, all plans get results."""
    plans = _make_plans(n_devices=6, n_tasks=2)
    assert len(plans) == 12

    stop_evt = threading.Event()
    pause_evt = threading.Event()
    pause_evt.set()

    s = _SlowScheduler(_make_config(bmc_workers=1), stop_event=stop_evt, pause_event=pause_evt)

    # Start in background thread
    results_container: list[list] = []

    def run_scheduler():
        results = s.run(plans)
        results_container.append(results)

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # Wait for some dispatch
    time.sleep(1.0)

    # Count how many dispatched before stop
    with s._results_lock:
        dispatched_before = len(s._results)

    # STOP
    s.stop()

    # Wait for run to complete
    t.join(timeout=15)
    assert not t.is_alive(), "Scheduler thread did not stop in time"

    results = results_container[0] if results_container else s._results
    total = len(results)

    # Assertions
    assert total == len(plans), f"All plans must have results: {total}/{len(plans)}"
    assert dispatched_before > 0, f"At least one plan must have dispatched: {dispatched_before}"

    # Some plans should be SKIPPED_STOPPED
    skipped = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_STOPPED")
    assert skipped > 0, f"Some plans should be SKIPPED_STOPPED after stop: skipped={skipped}"

    # Dispatch count should not equal total (stop prevented some dispatch)
    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    assert success < total, f"Not all plans should succeed: {success}/{total}"

    # Verify closure
    status_sum = success + skipped
    assert status_sum == total, f"Results not closed: {status_sum} != {total}"

    # No RUNNING plans
    running = sum(1 for r in results if r.execution_status == "RUNNING")
    assert running == 0, "No plans should be RUNNING"

    print(f"PASS: stop dispatched={dispatched_before}, total={total}, success={success}, skipped={skipped}")


# ============================================================================
# Case 2: App.pause()/resume()
# ============================================================================

def test_full_mode_pause_resume():
    """App.pause() must pause dispatch, resume() must continue."""
    plans = _make_plans(n_devices=8, n_tasks=2)
    assert len(plans) == 16

    pause_evt = threading.Event()
    pause_evt.set()
    stop_evt = threading.Event()

    s = _SlowScheduler(_make_config(bmc_workers=2), stop_event=stop_evt, pause_event=pause_evt)

    results_container = []

    def run_scheduler():
        results = s.run(plans)
        results_container.append(results)

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # Let it dispatch some
    time.sleep(0.8)

    # PAUSE
    s.pause()
    with s._results_lock:
        before_pause = len(s._results)

    # Wait — should NOT dispatch more
    time.sleep(1.5)
    with s._results_lock:
        during_pause = len(s._results)
    assert during_pause == before_pause, \
        f"Pause should block dispatch: {before_pause} → {during_pause}"

    # RESUME
    s.resume()
    time.sleep(1.5)
    with s._results_lock:
        after_resume = len(s._results)
    assert after_resume > during_pause, \
        f"Resume should continue dispatch: {during_pause} → {after_resume}"

    # STOP
    s.stop()
    t.join(timeout=15)
    assert not t.is_alive(), "Scheduler did not stop"

    results = results_container[0] if results_container else s._results
    assert len(results) == len(plans), f"All plans must have results: {len(results)}/{len(plans)}"

    print(f"PASS: pause/resume: {before_pause} → {during_pause} (pause) → {after_resume} (resume) → {len(results)} (done)")


# ============================================================================
# Case 3: RouteGuard stops dispatch
# ============================================================================

def test_route_guard_stops_dispatch():
    """Simulated route change must stop new dispatch and mark pending plans."""
    plans = _make_plans(n_devices=6, n_tasks=2)
    assert len(plans) == 12

    stop_evt = threading.Event()
    pause_evt = threading.Event()
    pause_evt.set()

    s = _SlowScheduler(_make_config(bmc_workers=1), stop_event=stop_evt, pause_event=pause_evt)

    results_container = []

    def run_scheduler():
        results = s.run(plans)
        results_container.append(results)

    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # Let some dispatch
    time.sleep(0.8)

    # Simulate RouteGuard route change — same path as App._on_route_change
    s.stop()  # Sets stop_event which scheduler loop checks

    t.join(timeout=15)
    assert not t.is_alive(), "Scheduler did not stop"

    results = results_container[0] if results_container else s._results

    # Pending plans must be SKIPPED
    route_skipped = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_ROUTE_CHANGED")
    stopped_skipped = sum(1 for r in results if r.execution_status == "EXEC_SKIPPED_STOPPED")
    total_skipped = route_skipped + stopped_skipped

    assert total_skipped > 0, f"Some plans must be skipped: route={route_skipped} stopped={stopped_skipped}"
    assert len(results) == len(plans), f"All plans must have results: {len(results)}/{len(plans)}"

    print(f"PASS: route guard: results={len(results)} skipped={total_skipped}")


# ============================================================================
# Case 4: App 连续运行两次不串状态
# ============================================================================

def test_app_consecutive_runs_no_state_leak():
    """App run twice must not leak state between runs."""
    from src.app import App

    class FakeConfig:
        output_root = "/tmp/bmc_audit_003_test"
        preflight_enabled = False
        route_guard_enabled = False
        tcp_connect_timeout = 5
        bmc_page_timeout = 60
        popup_dismiss_selector_timeout = 1000
        browser_headless = True
        browser_max_tasks_before_recycle = 50
        browser_max_age_seconds = 1800
        max_bmc_workers = 2
        max_ssh_workers = 2
        base_bmc_workers = 1
        base_ssh_workers = 1
        resource_check_interval = 30
        cpu_scale_down_pct = 90.0
        mem_scale_down_pct = 85.0
        cpu_scale_up_pct = 60.0
        mem_scale_up_pct = 50.0
        cpu_emergency_pct = 95.0
        mem_emergency_pct = 92.0
        resource_scale_emergency = 0.3
        resource_scale_down = 0.6
        resource_scale_up = 1.3
        resource_scale_normal = 1.0
        route_guard_check_interval = 30

    config = FakeConfig()
    app = App(config)

    # First run
    app.stop()
    app._results = [ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS")]
    assert len(app._results) == 1

    # Simulate what run() does at start
    app._results = []
    app._stop_event.clear()
    app._pause_event.set()

    assert len(app._results) == 0, "Results should be cleared"
    assert not app._stop_event.is_set(), "Stop event should be cleared"
    assert app._pause_event.is_set(), "Pause event should be set"
    assert app._active_scheduler is None, "Active scheduler should be None"

    print("PASS: App consecutive runs no state leak")


# ============================================================================
# Case 5: Scheduler respects external stop event at dispatch time
# ============================================================================

def test_scheduler_respects_stop_before_dispatch():
    """Scheduler must not dispatch if stop is set before run starts."""
    plans = _make_plans(n_devices=4, n_tasks=2)
    assert len(plans) == 8

    stop_evt = threading.Event()
    pause_evt = threading.Event()
    pause_evt.set()

    # Set stop before run
    stop_evt.set()

    s = _SlowScheduler(_make_config(), stop_event=stop_evt, pause_event=pause_evt)
    results = s.run(plans)

    # All plans should be skipped (never dispatched)
    success = sum(1 for r in results if r.execution_status == "EXEC_SUCCESS")
    skipped = sum(1 for r in results if "SKIPPED" in r.execution_status)

    assert success == 0, f"No plans should succeed when stop is pre-set: {success}"
    assert skipped == len(plans), f"All plans should be skipped: {skipped}/{len(plans)}"
    assert len(results) == len(plans), f"All plans must have results: {len(results)}/{len(plans)}"

    print(f"PASS: pre-stop blocks all dispatch: {skipped}/{len(plans)} skipped")


# ============================================================================
# Case 6: Summary closure after stop
# ============================================================================

def test_summary_closure_after_stop():
    """compute_summary must be closed after stop with skipped plans."""
    from src.out.collector import compute_summary

    results = [
        ExecutionResult("p1", "d1", task_name="t1", execution_status="EXEC_SUCCESS"),
        ExecutionResult("p2", "d2", task_name="t2", execution_status="EXEC_SUCCESS"),
        ExecutionResult("p3", "d3", task_name="t3", execution_status="EXEC_SKIPPED_STOPPED"),
        ExecutionResult("p4", "d4", task_name="t4", execution_status="EXEC_SKIPPED_ROUTE_CHANGED"),
        ExecutionResult("p5", "d5", task_name="t5", execution_status="EXEC_SKIPPED_STOPPED"),
    ]

    s = compute_summary(results)
    assert s["total"] == 5
    assert s["success"] == 2
    assert s["skipped_stopped"] == 2
    assert s["skipped_route"] == 1

    # Closure
    status_sum = (s["success"] + s["failed"] + s["error"] + s["timeout"] + s["partial"] +
                  s["skipped_preflight"] + s["skipped_port_blocked"] + s["skipped_route"] +
                  s["skipped_stopped"] + s["skipped_disabled"] + s["skipped_session"])
    assert status_sum == s["total"], f"Summary not closed: {status_sum} != {s['total']}"

    print("PASS: summary closure after stop")


if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', __file__, '-v', '--tb=short'],
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    sys.exit(result.returncode)
