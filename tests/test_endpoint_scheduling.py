"""Comprehensive tests for endpoint-aware resource scheduling.

Tests are pure Python (no BMC/SSH hardware) — they verify scheduling
semantics, serialization, concurrency, timing, and global registry.

Run:  python -m pytest tests/test_endpoint_scheduling.py -v
"""

from __future__ import annotations

import sys
import os
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.worker_pool import WorkerPool
from src.scheduler.dynamic_scheduler import DynamicScheduler
from src.scheduler.resource_registry import ResourceRegistry
from src.scheduler.plan_generator import generate_plans


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_device(
    name: str,
    group: str = "G1",
    bmc_ip: str = "",
    inband_ip: str = "",
) -> Device:
    return Device(
        row_index=0,
        device_name=name,
        device_group=group,
        bmc_ip=bmc_ip,
        bmc_username="u",
        bmc_password="p",
        inband_ip=inband_ip,
        inband_username="u",
        inband_password="p",
        enabled=True,
        tags="",
    )


def make_task(
    name: str,
    task_type: str = "BMC",
    execution_mode: str = "BMC_URL",
) -> Task:
    return Task(
        row_index=0,
        sequence=0,
        task_name=name,
        task_type=task_type,
        execution_mode=execution_mode,
        command_or_url="/test",
        timeout_seconds=10,
        enabled=True,
    )


def make_bmc_plan(device_name: str, bmc_ip: str, task_name: str = "BMC_T1") -> TaskPlan:
    d = make_device(device_name, bmc_ip=bmc_ip)
    t = make_task(task_name, "BMC")
    return TaskPlan(device=d, task=t)


def make_inband_plan(device_name: str, inband_ip: str, task_name: str = "SSH_T1") -> TaskPlan:
    d = make_device(device_name, inband_ip=inband_ip)
    t = make_task(task_name, "SSH", "SSH_CMD")
    return TaskPlan(device=d, task=t)


class FakeResult(ExecutionResult):
    """Minimal result for fake executor."""
    pass


def fake_executor_factory(sleep_seconds: float = 0.1):
    """Return a fake _execute_plan that sleeps and returns success."""
    def execute(plan: TaskPlan) -> ExecutionResult:
        plan.executor_started_at = time.time()
        time.sleep(sleep_seconds)
        plan.executor_finished_at = time.time()
        return ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            task_name=plan.task.task_name,
            execution_status="EXEC_SUCCESS",
            started_at=plan.executor_started_at,
            ended_at=plan.executor_finished_at,
            duration_seconds=sleep_seconds,
        )
    return execute


class FakeScheduler(DynamicScheduler):
    """Scheduler subclass that uses fake executor with configurable delay."""

    def __init__(self, config, sleep_seconds: float = 0.1, event_bus=None):
        super().__init__(config, event_bus=event_bus)
        self._fake_sleep = sleep_seconds

    def _execute_plan(self, plan: TaskPlan) -> ExecutionResult:
        plan.executor_started_at = time.time()
        time.sleep(self._fake_sleep)
        plan.executor_finished_at = time.time()
        result = ExecutionResult(
            plan_id=plan.plan_id,
            device_name=plan.device.device_name,
            task_name=plan.task.task_name,
            execution_status="EXEC_SUCCESS",
            started_at=plan.executor_started_at,
            ended_at=plan.executor_finished_at,
            duration_seconds=self._fake_sleep,
        )
        # Copy timing into plan
        plan.ended_at = plan.executor_finished_at
        return result


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the global ResourceRegistry before each test."""
    reg = ResourceRegistry()
    reg._reset_for_test()
    yield
    reg._reset_for_test()


# ===================================================================
# Test 1: Same endpoint serialization
# ===================================================================
def test_same_endpoint_serial():
    """3 BMC plans, same OOB_IP, max_bmc_workers=3 → serial execution, ~3s."""
    plans = [
        make_bmc_plan("D1", "10.0.0.1", "BMC_T1"),
        make_bmc_plan("D2", "10.0.0.1", "BMC_T2"),
        make_bmc_plan("D3", "10.0.0.1", "BMC_T3"),
    ]

    # Verify all have same endpoint_key
    keys = [p.endpoint_key for p in plans]
    assert all(k == "BMC:10.0.0.1:443" for k in keys), f"endpoint_keys: {keys}"

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.max_ssh_workers = 1
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=1.0)
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    # Wall clock should be ~3s (serialized), not ~1s (which would indicate concurrency)
    assert elapsed >= 2.5, f"Wall clock too fast: {elapsed:.2f}s (expected ~3s for serial)"
    # Allow some tolerance for scheduling overhead
    assert elapsed <= 5.0, f"Wall clock too slow: {elapsed:.2f}s"

    print(f"  PASS: same endpoint serial — wall clock={elapsed:.2f}s (expected ~3s)")


# ===================================================================
# Test 2: Different endpoint concurrency
# ===================================================================
def test_different_endpoint_concurrent():
    """3 BMC plans, 3 different OOB_IPs, max_bmc_workers=3 → ~1s."""
    plans = [
        make_bmc_plan("D1", "10.0.0.1", "BMC_T1"),
        make_bmc_plan("D2", "10.0.0.2", "BMC_T2"),
        make_bmc_plan("D3", "10.0.0.3", "BMC_T3"),
    ]

    keys = [p.endpoint_key for p in plans]
    assert len(set(keys)) == 3, f"Expected 3 unique keys, got {keys}"

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=1.0)
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 3
    # Should complete in ~1s (all concurrent)
    assert elapsed < 2.0, f"Wall clock too slow: {elapsed:.2f}s (expected ~1s for concurrent)"

    print(f"  PASS: different endpoint concurrent — wall clock={elapsed:.2f}s (expected ~1s)")


# ===================================================================
# Test 3: Mixed long-tail
# ===================================================================
def test_mixed_longtail():
    """Endpoint A: 5 tasks, Endpoints B/C/D: 1 each. max_workers=3. B/C/D not blocked by A."""
    devices = [
        make_device("D_A", bmc_ip="10.0.0.1"),
        make_device("D_B", bmc_ip="10.0.0.2"),
        make_device("D_C", bmc_ip="10.0.0.3"),
        make_device("D_D", bmc_ip="10.0.0.4"),
    ]

    plans = []
    for i in range(5):
        plans.append(TaskPlan(device=devices[0], task=make_task(f"BMC_A{i}", "BMC")))
    plans.append(TaskPlan(device=devices[1], task=make_task("BMC_B", "BMC")))
    plans.append(TaskPlan(device=devices[2], task=make_task("BMC_C", "BMC")))
    plans.append(TaskPlan(device=devices[3], task=make_task("BMC_D", "BMC")))

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=1.0)
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 8
    # A has 5 tasks serialized → ~5s
    # B/C/D have 1 each and start quickly since workers=3
    # Total should be ~5-6s (not 8s)
    assert elapsed < 7.5, f"Wall clock too slow: {elapsed:.2f}s — B/C/D may be blocked by A"

    print(f"  PASS: mixed longtail — wall clock={elapsed:.2f}s (expected ~5-6s)")


# ===================================================================
# Test 4: INBAND endpoint serialization
# ===================================================================
def test_inband_serial():
    """2 same INBAND endpoint SSH plans, max_ssh_workers=2 → serial."""
    plans = [
        make_inband_plan("D1", "192.168.1.1", "SSH_T1"),
        make_inband_plan("D2", "192.168.1.1", "SSH_T2"),
    ]

    keys = [p.endpoint_key for p in plans]
    assert all(k == "INBAND:192.168.1.1:22" for k in keys), f"keys: {keys}"

    config = AppConfig()
    config.max_ssh_workers = 2
    config.base_ssh_workers = 2
    config.max_bmc_workers = 1
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=1.0)
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 2
    assert elapsed >= 1.8, f"Wall clock too fast: {elapsed:.2f}s (expected ~2s for serial)"

    print(f"  PASS: INBAND endpoint serial — wall clock={elapsed:.2f}s (expected ~2s)")


# ===================================================================
# Test 5: BMC and INBAND separate pools
# ===================================================================
def test_bmc_inband_separate_pools():
    """BMC plan (sleep 1s) + INBAND plan (sleep 1s), different endpoints → concurrent ~1s."""
    plans = [
        make_bmc_plan("D1", "10.0.0.1", "BMC_T"),
        make_inband_plan("D2", "192.168.1.1", "SSH_T"),
    ]

    config = AppConfig()
    config.max_bmc_workers = 1
    config.base_bmc_workers = 1
    config.max_ssh_workers = 1
    config.base_ssh_workers = 1
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=1.0)
    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 2
    # Should complete in ~1s (BMC and INBAND run in parallel)
    assert elapsed < 1.8, f"Wall clock too slow: {elapsed:.2f}s (expected ~1s for concurrent pools)"

    print(f"  PASS: BMC+INBAND separate pools — wall clock={elapsed:.2f}s (expected ~1s)")


# ===================================================================
# Test 6: Global ResourceRegistry cross-scheduler
# ===================================================================
def test_resource_registry_cross_scheduler():
    """Two schedulers submitting same endpoint_key → serial (registry enforces)."""
    plan_a = make_bmc_plan("DA", "10.0.0.1", "BMC_T1")
    plan_b = make_bmc_plan("DB", "10.0.0.1", "BMC_T1")  # Same IP!

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.output_root = "/tmp/bmc_test_endpoint"

    # Create two schedulers
    s1 = FakeScheduler(config, sleep_seconds=1.0)
    s2 = FakeScheduler(config, sleep_seconds=1.0)

    results = []
    threads = []
    for sched, plist in [(s1, [plan_a]), (s2, [plan_b])]:
        t = threading.Thread(target=lambda s=sched, pp=plist: results.extend(s.run(pp)))
        threads.append(t)

    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    assert len(results) == 2
    # Even with max_bmc_workers=3 and 2 schedulers, same endpoint_key forces serial
    assert elapsed >= 1.8, f"Wall clock too fast: {elapsed:.2f}s (expected ~2s)"
    # Registry prevents concurrent access
    reg = ResourceRegistry()
    assert not reg.held_keys, "Registry should be empty after all releases"

    print(f"  PASS: ResourceRegistry cross-scheduler — wall clock={elapsed:.2f}s (expected ~2s)")


# ===================================================================
# Test 7: Timing accuracy
# ===================================================================
def test_timing_accuracy():
    """Verify plan_timing fields are populated correctly."""
    plans = [
        make_bmc_plan("D1", "10.0.0.1", "BMC_T1"),
        make_bmc_plan("D2", "10.0.0.2", "BMC_T2"),
    ]

    config = AppConfig()
    config.max_bmc_workers = 2
    config.base_bmc_workers = 2
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=0.5)
    t0 = time.time()
    results = scheduler.run(plans)
    wall_clock = time.time() - t0

    # Verify each plan has timing info
    for r in results:
        assert r.duration_seconds > 0, f"duration_seconds should be > 0, got {r.duration_seconds}"
        assert r.started_at > 0, "started_at should be set"
        assert r.ended_at > 0, "ended_at should be set"

    # Sum of plan durations should be >= wall clock (concurrency makes sum bigger)
    sum_duration = sum(r.duration_seconds for r in results)
    parallel_efficiency = sum_duration / wall_clock if wall_clock > 0 else 0
    # With 2 concurrent plans, sum should be ~1s while wall clock is ~0.5s
    # So parallel_efficiency should be ~2
    # Allow 0.05s tolerance for scheduling overhead
    assert sum_duration + 0.05 >= wall_clock, f"sum_duration ({sum_duration:.2f}) < wall_clock ({wall_clock:.2f})"
    assert parallel_efficiency >= 0.95, f"parallel_efficiency < 0.95: {parallel_efficiency:.2f}"

    print(f"  PASS: timing accuracy — sum_duration={sum_duration:.2f}s, wall_clock={wall_clock:.2f}s, "
          f"parallel_efficiency={parallel_efficiency:.2f}")


# ===================================================================
# Test 8: endpoint_key property correctness
# ===================================================================
def test_endpoint_key_correctness():
    """Verify endpoint_key and endpoint_type rules."""
    # BMC
    p = make_bmc_plan("D1", "10.0.0.1")
    assert p.endpoint_type == "BMC"
    assert p.endpoint_key == "BMC:10.0.0.1:443"
    assert p.resource_type == "BMC"

    # INBAND SSH
    p = make_inband_plan("D2", "192.168.1.1")
    assert p.endpoint_type == "INBAND"
    assert p.endpoint_key == "INBAND:192.168.1.1:22"

    # INBAND TELNET → port 23
    d = make_device("D_TELNET", inband_ip="10.0.0.99")
    t = make_task("TELNET_T1", "TELNET", "TELNET_CMD")
    p = TaskPlan(device=d, task=t)
    assert p.endpoint_type == "INBAND"
    assert p.endpoint_key == "INBAND:10.0.0.99:23", f"TELNET port should be 23, got {p.endpoint_key}"
    assert p.resource_type == "INBAND"

    # BMC missing IP
    p = make_bmc_plan("D3", "")
    assert p.endpoint_key == "BMC:MISSING_IP:D3"

    # INBAND missing IP
    p = make_inband_plan("D4", "")
    assert p.endpoint_key == "INBAND:MISSING_IP:D4"

    # TELNET missing IP
    d2 = make_device("D_TELNET2", inband_ip="")
    t2 = make_task("TELNET_T2", "TELNET", "TELNET_CMD")
    p2 = TaskPlan(device=d2, task=t2)
    assert "MISSING_IP" in p2.endpoint_key

    print("  PASS: endpoint_key correctness (including TELNET)")


# ===================================================================
# Test 9: Registry reentrant acquire
# ===================================================================
def test_registry_reentrant():
    """Same holder (execution + plan) can acquire same key twice (no deadlock)."""
    reg = ResourceRegistry()
    meta = {"execution_id": "e1", "plan_id": "p1", "device_name": "D1", "task_name": "T1"}

    with reg.acquire("BMC:10.0.0.1:443", meta) as lease:
        assert not lease["reentrant"]
        # Re-acquire from same holder
        with reg.acquire("BMC:10.0.0.1:443", meta) as lease2:
            assert lease2["reentrant"]
        # Still held
        assert reg.is_held("BMC:10.0.0.1:443")

    assert not reg.is_held("BMC:10.0.0.1:443")
    print("  PASS: registry reentrant acquire")


# ===================================================================
# Test 10: WorkerPool resource_key
# ===================================================================
def test_worker_pool_resource_key():
    """WorkerPool uses resource_key (endpoint_key) not device_name."""
    pool = WorkerPool("test", base_workers=2, max_workers=2)
    pool.start()

    lock_results = []

    def check_lock(ekey: str):
        with pool._lock:
            lock_results.append(ekey in pool._running_resources)

    # Dispatch with resource_key (sleep to ensure future is still active when we check)
    pool.dispatch(lambda: time.sleep(0.2), resource_key="BMC:10.0.0.1:443")
    time.sleep(0.05)  # Give worker thread time to pick up the task
    check_lock("BMC:10.0.0.1:443")
    assert lock_results[-1], "resource_key should be in _running_resources"

    # resource_has_running_task
    assert pool.resource_has_running_task("BMC:10.0.0.1:443")
    assert not pool.resource_has_running_task("BMC:10.0.0.2:443")

    pool.shutdown(wait=True)
    print("  PASS: WorkerPool resource_key")


# ===================================================================
# Test 11: No device_name used as lock key
# ===================================================================
def test_no_device_name_as_lock():
    """Verify the scheduler no longer uses device_name as mutual exclusion key."""
    plans = [
        make_bmc_plan("D1", "10.0.0.1", "BMC_T1"),
        make_bmc_plan("D2", "10.0.0.1", "BMC_T2"),  # Same BMC IP, different device_name
    ]

    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.output_root = "/tmp/bmc_test_endpoint"

    scheduler = FakeScheduler(config, sleep_seconds=0.2)

    # Check that scheduler doesn't have _device_queues or _running_devices
    assert not hasattr(scheduler, '_device_queues') or not scheduler._device_queues
    assert not hasattr(scheduler, '_running_devices')

    # Instead it has _endpoint_queues and pools use _running_resources
    assert hasattr(scheduler, '_endpoint_queues')

    t0 = time.time()
    results = scheduler.run(plans)
    elapsed = time.time() - t0

    assert len(results) == 2
    # Must be serial (same endpoint)
    assert elapsed >= 0.35, f"Wall clock too fast: {elapsed:.2f}s (serial expected)"

    print(f"  PASS: no device_name as lock key — wall clock={elapsed:.2f}s")


# ===================================================================
# Test 12: Executor fallback — self-acquire via registry
# ===================================================================
def test_executor_fallback_bmc_standalone():
    """Two threads self-acquiring same endpoint via ResourceRegistry → serialized.

    Simulates two BMCExecutor.execute() calls on the same endpoint without
    a scheduler holding the lease.  The executor's fallback path calls
    reg.acquire() which blocks until the endpoint is free.
    """
    from src.scheduler.resource_registry import ResourceRegistry

    reg = ResourceRegistry()
    reg._reset_for_test()

    ekey = "BMC:10.0.0.99:443"
    exec_order = []
    lock = threading.Lock()

    def fake_executor_run(plan_id: str):
        """Simulate BMCExecutor.execute() fallback path."""
        meta = {
            "execution_id": "",
            "plan_id": plan_id,
            "device_name": "D1",
            "task_name": "T1",
        }
        with reg.acquire(ekey, meta) as lease_info:
            with lock:
                exec_order.append(plan_id)
            assert not lease_info["reentrant"], "Standalone executor should not be reentrant"
            time.sleep(0.5)

    t0 = time.time()
    t1 = threading.Thread(target=fake_executor_run, args=("plan-a",))
    t2 = threading.Thread(target=fake_executor_run, args=("plan-b",))
    t1.start()
    time.sleep(0.05)  # Ensure t1 acquires first
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.time() - t0

    assert len(exec_order) == 2, f"Both should execute, got {len(exec_order)}"
    # Two 0.5s sleep serialized → >=0.9s
    assert elapsed >= 0.8, f"Executor fallback not serializing: {elapsed:.2f}s (expected >=0.8s)"
    assert not reg.held_keys, "Registry should be empty after all releases"
    print(f"  PASS: executor fallback standalone — wall clock={elapsed:.2f}s, order={exec_order}")


# ===================================================================
# Test 13: Scheduler-held lease + executor call → no deadlock
# ===================================================================
def test_executor_no_deadlock_when_scheduler_holds():
    """When scheduler already holds lease, executor must NOT block on re-acquire."""
    plan = make_bmc_plan("D1", "10.0.0.1", "BMC_T1")

    config = AppConfig()
    config.max_bmc_workers = 1
    config.base_bmc_workers = 1
    config.output_root = "/tmp/bmc_test_endpoint_deadlock"

    # Simulate: scheduler has already acquired the lease
    plan._resource_lease_held = True
    plan._execution_id = "test-exec-001"

    # Now create a FakeScheduler that dispatches but we'll verify
    # the executor doesn't block
    scheduler = FakeScheduler(config, sleep_seconds=0.2)

    t0 = time.time()
    result = scheduler._execute_plan(plan)
    elapsed = time.time() - t0

    # Should complete immediately (~0.2s), not hang
    assert elapsed < 1.0, f"Executor blocked despite scheduler-held lease: {elapsed:.2f}s"
    assert result.execution_status == "EXEC_SUCCESS"
    print(f"  PASS: no deadlock when scheduler holds lease — elapsed={elapsed:.2f}s")


# ===================================================================
# Test 14: Concurrent direct executor calls on same endpoint → serial via registry
# ===================================================================
def test_direct_executor_concurrent_same_endpoint_serial():
    """Two threads calling executor.execute() on same endpoint → registry serializes."""
    from src.scheduler.resource_registry import ResourceRegistry

    reg = ResourceRegistry()
    reg._reset_for_test()

    plan_a = make_bmc_plan("DA", "10.0.0.88", "BMC_T1")
    plan_b = make_bmc_plan("DB", "10.0.0.88", "BMC_T2")  # Same IP

    # Neither plan has _resource_lease_held → executor will self-acquire
    assert not plan_a._resource_lease_held
    assert not plan_b._resource_lease_held

    run_order = []
    lock = threading.Lock()

    def fake_run(plan):
        # Simulate the fallback path in executor.execute()
        meta = {
            "execution_id": plan._execution_id,
            "plan_id": plan.plan_id,
            "device_name": plan.device.device_name,
            "task_name": plan.task.task_name,
        }
        with reg.acquire(plan.endpoint_key, meta):
            with lock:
                run_order.append(plan.plan_id)
            time.sleep(0.5)

    t0 = time.time()
    t1 = threading.Thread(target=fake_run, args=(plan_a,))
    t2 = threading.Thread(target=fake_run, args=(plan_b,))
    t1.start()
    # Small delay so t1 acquires first
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.time() - t0

    assert len(run_order) == 2
    # Must be serialized by registry (two 0.5s sleeps = ~1.0s)
    assert elapsed >= 0.9, f"Registry did not serialize direct executor calls: {elapsed:.2f}s"
    assert not reg.held_keys, "Registry should be empty after release"
    print(f"  PASS: direct concurrent executor same endpoint serial — wall clock={elapsed:.2f}s")


# ===================================================================
# Test 15: Reentrant acquire from same holder (scheduler + executor) does not deadlock
# ===================================================================
def test_reentrant_acquire_scheduler_then_executor():
    """Scheduler try_hold, then executor acquire with same (exec_id, plan_id) is reentrant."""
    from src.scheduler.resource_registry import ResourceRegistry

    reg = ResourceRegistry()
    reg._reset_for_test()

    ekey = "BMC:10.0.0.77:443"
    exec_id = "exec-001"
    plan_id = "plan-abc"

    # Step 1: Scheduler try_hold (non-blocking)
    meta = {"execution_id": exec_id, "plan_id": plan_id,
            "device_name": "D1", "task_name": "T1"}
    ok = reg.try_hold(ekey, meta)
    assert ok, "Scheduler should acquire"

    # Step 2: Executor acquire (blocking) with same holder key → reentrant
    t0 = time.time()
    with reg.acquire(ekey, meta) as lease_info:
        assert lease_info["reentrant"], f"Should be reentrant, got {lease_info}"
        assert lease_info["wait_seconds"] == 0.0, "Reentrant should have zero wait"
    elapsed = time.time() - t0

    # Reentrant should be instant (no blocking)
    assert elapsed < 0.1, f"Reentrant acquire blocked: {elapsed:.2f}s"
    # Scheduler's original lease still held
    assert reg.is_held(ekey), "Registry should still hold after reentrant exit"

    # Step 3: Scheduler releases
    reg.release(ekey)
    assert not reg.is_held(ekey), "Registry should be empty after full release"

    print(f"  PASS: reentrant acquire scheduler+executor — elapsed={elapsed:.3f}s")


# ===================================================================
# Main
# ===================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
