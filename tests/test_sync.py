"""Test dispatch logic WITHOUT threads - pure sequential."""
from __future__ import annotations
import pytest
import sys, time
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.execution_result import ExecutionResult
from src.scheduler.worker_pool import WorkerPool
from src.scheduler.plan_generator import generate_plans


# --- Helpers ---

def _build_device_queues(plans):
    device_queues: dict[str, deque] = {}
    for plan in plans:
        did = plan.device_id
        if did not in device_queues:
            device_queues[did] = deque()
        device_queues[did].append(plan)
    return device_queues


def _simulate_dispatch_loop(device_queues):
    """Simulate EXACT dispatch/completion logic — pure sequential, no threads."""
    ready_devices: deque[str] = deque()
    for did in device_queues:
        ready_devices.append(did)

    results = []
    bmc_pool = WorkerPool("bmc", 1, 1)
    ssh_pool = WorkerPool("ssh", 1, 1)
    bmc_pool.start()
    ssh_pool.start()

    dispatched = 0
    _completed = 0

    def execute_plan(plan):
        nonlocal dispatched
        dispatched += 1
        return ExecutionResult(
            plan_id=plan.plan_id, device_name=plan.device.device_name,
            task_name=plan.task.task_name, execution_status="EXEC_SUCCESS",
            started_at=time.time(), ended_at=time.time(),
        )

    def on_complete(result, plan, did):
        nonlocal _completed
        _completed += 1
        results.append(result)
        q = device_queues.get(did)
        if q:
            ready_devices.append(did)

    iteration = 0
    while any(q for q in device_queues.values()):
        iteration += 1
        made_progress = False
        for pool, protocol in [(bmc_pool, "BMC"), (ssh_pool, "SSH")]:
            skipped = 0
            ready_snapshot = len(ready_devices)
            while pool.has_idle() and ready_devices:
                if skipped >= ready_snapshot:
                    break
                did = ready_devices.popleft()
                q = device_queues.get(did)
                if not q:
                    continue
                plan = q[0]
                if plan.protocol != protocol:
                    ready_devices.append(did)
                    skipped += 1
                    continue
                plan = q.popleft()
                result = execute_plan(plan)
                on_complete(result, plan, did)
                made_progress = True
                skipped = 0
        if not made_progress:
            break

    bmc_pool.shutdown(wait=True)
    ssh_pool.shutdown(wait=True)
    return results, device_queues


# --- Tests ---

def test_dispatch_loop_clean():
    """2 devices × 2 tasks — dispatch loop must complete all plans."""
    devices = [
        Device(0, "D0", "G1", "10.0.0.1", "a", "p", "10.0.0.101", "u", "p", True, ()),
        Device(1, "D1", "G1", "10.0.1.1", "a", "p", "10.0.1.101", "u", "p", True, ()),
    ]
    tasks = [
        Task(0, 0, "BMC_T0", "BMC", "BMC_URL", "", (), "/test", timeout_seconds=5, enabled=True),
        Task(1, 1, "SSH_T0", "SSH", "SSH_CMD", "", (), "show ver", timeout_seconds=5, enabled=True),
    ]
    plans = generate_plans(devices, tasks)
    assert len(plans) == 4, f"Expected 4 plans, got {len(plans)}"

    device_queues = _build_device_queues(plans)
    results, remaining_queues = _simulate_dispatch_loop(device_queues)

    remaining = sum(len(q) for q in remaining_queues.values())
    assert len(results) == len(plans), f"Missing results: {len(results)}/{len(plans)}"
    assert remaining == 0, f"Remaining in queues: {remaining}"
    print(f"PASS: {len(results)}/{len(plans)} results")


# --- WorkerPool shutdown timeout tests ---

def test_shutdown_idle_pool():
    """shutdown(wait=True) on an idle pool must return immediately and gracefully."""
    pool = WorkerPool("test_idle", 1, 1)
    pool.start()
    t0 = time.time()
    result = pool.shutdown(wait=True, shutdown_timeout=10)
    elapsed = time.time() - t0
    assert result.graceful, f"Expected graceful shutdown, got {result}"
    assert not result.timed_out, f"Should not time out: {result}"
    assert elapsed < 5, f"Should complete quickly: {elapsed:.2f}s"
    print(f"PASS: idle shutdown graceful={result.graceful} ({elapsed:.3f}s)")


def test_shutdown_twice_idempotent():
    """Calling shutdown() multiple times must not hang or error."""
    pool = WorkerPool("test_idempotent", 1, 1)
    pool.start()
    r1 = pool.shutdown(wait=True, shutdown_timeout=10)
    assert r1.graceful, f"First shutdown failed: {r1}"

    # Second shutdown on same pool must return immediately (already shut down)
    t0 = time.time()
    r2 = pool.shutdown(wait=True, shutdown_timeout=10)
    elapsed2 = time.time() - t0
    assert r2.graceful, f"Second shutdown should be no-op graceful: {r2}"
    assert elapsed2 < 1, f"Second shutdown should be instant: {elapsed2:.3f}s"

    # Third shutdown
    r3 = pool.shutdown(wait=False)
    assert r3.graceful
    print("PASS: shutdown idempotent (3 consecutive calls)")


def test_shutdown_wait_false_returns_immediately():
    """shutdown(wait=False) must return without blocking."""
    pool = WorkerPool("test_nowait", 1, 1)
    pool.start()
    t0 = time.time()
    r = pool.shutdown(wait=False)
    elapsed = time.time() - t0
    assert elapsed < 1, f"wait=False must be fast: {elapsed:.3f}s"
    assert not r.graceful, "wait=False is not graceful"
    # Idempotent
    r2 = pool.shutdown(wait=False)
    assert r2.graceful, "Already-shut-down pool returns graceful"
    print(f"PASS: wait=False immediate ({elapsed:.3f}s)")


def test_shutdown_timeout_cancels_pending():
    """A long-running future should timeout and not block the caller."""
    pool = WorkerPool("test_timeout", 1, 1)
    pool.start()

    marker = {"started": False}

    def long_task():
        marker["started"] = True
        time.sleep(60)  # never finishes

    pool.dispatch(long_task, resource_key="test-endpoint")

    # Give the future a moment to start
    time.sleep(0.3)
    assert marker["started"], "Task should have started"

    t0 = time.time()
    result = pool.shutdown(wait=True, shutdown_timeout=1.0)
    elapsed = time.time() - t0

    assert result.timed_out, f"Expected timeout: {result}"
    assert not result.graceful, "Should not be graceful"
    assert elapsed >= 0.5, f"Should wait at least ~1s: {elapsed:.2f}s"
    assert elapsed < 5, f"Must not hang forever: {elapsed:.2f}s"
    print(f"PASS: timeout after {elapsed:.2f}s result={result}")


def test_shutdown_does_not_corrupt_dispatch_results():
    """Fast tasks dispatched before shutdown must complete and collect results."""
    pool = WorkerPool("test_results", 4, 4)
    pool.start()

    collected = []

    def capture(r):
        collected.append(r)

    for i in range(10):
        pool.dispatch(
            lambda v=i: v,
            resource_key=f"ep-{i}",
            on_complete=lambda r: collected.append(r),
        )

    time.sleep(2.0)  # let all fast tasks complete

    r = pool.shutdown(wait=True, shutdown_timeout=10)
    # At minimum 10 results should be collected; some may be the lambda return values
    assert len(collected) >= 10, f"Expected ≥10 results, got {len(collected)}"
    assert r.graceful, f"Shutdown should be graceful: {r}"
    print(f"PASS: {len(collected)} results collected, shutdown graceful")


if __name__ == "__main__":
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
    device_queues = _build_device_queues(plans)
    print(f"Devices: {list(device_queues.keys())}")
    ready_devices = deque(device_queues.keys())
    print(f"Ready: {list(ready_devices)}")
    for did, q in device_queues.items():
        print(f"  {did}: {[p.task.task_name for p in q]}")
    results, remaining_queues = _simulate_dispatch_loop(device_queues)
    remaining = sum(len(q) for q in remaining_queues.values())
    print(f"\nFinal: {len(results)}/{len(plans)} results")
    print(f"Remaining in queues: {remaining}")
    passed = len(results) == len(plans) and remaining == 0
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)
