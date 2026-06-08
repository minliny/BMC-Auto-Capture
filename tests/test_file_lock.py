"""Tests for cross-process file lock.

Run: python -m pytest tests/test_file_lock.py -v
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.scheduler.file_lock import FileLock, _safe_filename


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_safe_filename():
    assert _safe_filename("BMC:10.0.0.1:443") == "BMC_10.0.0.1_443"
    assert _safe_filename("INBAND:192.168.1.1:22") == "INBAND_192.168.1.1_22"
    assert _safe_filename("INBAND:10.0.0.1:23") == "INBAND_10.0.0.1_23"
    assert _safe_filename("BMC:MISSING_IP:D1") == "BMC_MISSING_IP_D1"
    assert ".." not in _safe_filename("../etc/passwd")
    print("  PASS: safe_filename")


def test_acquire_release():
    lock = FileLock()
    ekey = "BMC:10.0.0.77:443"
    assert not lock.is_locked(ekey)

    with lock.acquire(ekey, timeout=1):
        assert lock.is_locked(ekey)

    assert not lock.is_locked(ekey)
    print("  PASS: acquire/release")


def test_try_acquire():
    lock = FileLock()
    ekey = "BMC:10.0.0.78:443"

    ctx = lock.try_acquire(ekey)
    assert ctx is not None, "Should acquire free lock"
    assert lock.is_locked(ekey)

    # Second try_acquire should fail
    ctx2 = lock.try_acquire(ekey)
    assert ctx2 is None, "Should not acquire held lock"

    ctx.__exit__()
    assert not lock.is_locked(ekey)
    print("  PASS: try_acquire blocking")


def test_different_endpoints_independent():
    lock = FileLock()
    a = "BMC:10.0.0.81:443"
    b = "BMC:10.0.0.82:443"

    with lock.acquire(a, timeout=1):
        assert lock.is_locked(a)
        # Different endpoint should NOT be blocked
        ctx_b = lock.try_acquire(b)
        assert ctx_b is not None, "Different endpoint should be free"
        ctx_b.__exit__()
    print("  PASS: different endpoints independent")


# ---------------------------------------------------------------------------
# Integration: ResourceRegistry + FileLock
# ---------------------------------------------------------------------------

def test_registry_with_file_lock():
    """ResourceRegistry with FileLock enabled serializes same endpoint."""
    from src.scheduler.resource_registry import ResourceRegistry

    # Enable file lock
    ResourceRegistry.enable_file_lock()
    reg = ResourceRegistry()
    reg._reset_for_test()

    ekey = "BMC:10.0.0.90:443"

    ok1 = reg.try_hold(ekey, {"plan_id": "p1"})
    assert ok1, "Should acquire"

    ok2 = reg.try_hold(ekey, {"plan_id": "p2"})
    assert not ok2, "Should be blocked by registry + file lock"

    reg.release(ekey)
    assert not reg.is_held(ekey)
    print("  PASS: registry with file lock")


# ---------------------------------------------------------------------------
# Subprocess test: two processes fight for same endpoint
# ---------------------------------------------------------------------------

def _subprocess_worker(ekey: str, lock_dir: str, result_queue: multiprocessing.Queue):
    """Worker process: acquire file lock, sleep, release, report timing."""
    lock = FileLock(lock_dir)
    t0 = time.time()
    with lock.acquire(ekey, timeout=10):
        acquired_at = time.time() - t0
        time.sleep(0.5)  # Simulate work
    total = time.time() - t0
    result_queue.put({"acquired_at": acquired_at, "total": total})


def test_two_processes_same_endpoint_serial():
    """Two subprocesses, same endpoint → serial execution."""
    import tempfile
    lock_dir = tempfile.mkdtemp(prefix="bmc_test_lock_")
    ekey = "BMC:10.0.0.99:443"

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()

    t0 = time.time()
    p1 = ctx.Process(target=_subprocess_worker, args=(ekey, lock_dir, q))
    p2 = ctx.Process(target=_subprocess_worker, args=(ekey, lock_dir, q))
    p1.start()
    time.sleep(0.05)  # Let p1 acquire first
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = []
    while not q.empty():
        results.append(q.get())

    elapsed = time.time() - t0
    assert len(results) == 2, f"Both processes should complete, got {len(results)}"
    # Serial: 0.5s sleep × 2 = ~1.0s + overhead
    assert elapsed >= 0.9, f"Processes not serialized: {elapsed:.2f}s (expected >=0.9s)"
    assert elapsed < 5.0, f"Processes took too long: {elapsed:.2f}s"

    # Clean up lock files
    import shutil
    try:
        shutil.rmtree(lock_dir)
    except Exception:
        pass

    print(f"  PASS: two processes serial — wall_clock={elapsed:.2f}s")


def test_two_processes_different_endpoint_concurrent():
    """Two subprocesses, different endpoints → concurrent execution."""
    import tempfile
    lock_dir = tempfile.mkdtemp(prefix="bmc_test_lock_")
    ekey_a = "BMC:10.0.0.98:443"
    ekey_b = "BMC:10.0.0.97:443"

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()

    t0 = time.time()
    p1 = ctx.Process(target=_subprocess_worker, args=(ekey_a, lock_dir, q))
    p2 = ctx.Process(target=_subprocess_worker, args=(ekey_b, lock_dir, q))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = []
    while not q.empty():
        results.append(q.get())

    elapsed = time.time() - t0
    assert len(results) == 2
    # Concurrent: 0.5s sleep, different endpoints → ~0.5s
    assert elapsed < 1.5, f"Different endpoints should be concurrent: {elapsed:.2f}s"

    import shutil
    try:
        shutil.rmtree(lock_dir)
    except Exception:
        pass

    print(f"  PASS: two processes different endpoints concurrent — wall_clock={elapsed:.2f}s")


# ---------------------------------------------------------------------------
# ResourceRegistry with file lock + cross-scheduler subprocess
# ---------------------------------------------------------------------------

def _registry_subprocess_worker(ekey: str, result_queue: multiprocessing.Queue):
    """Worker that uses ResourceRegistry + FileLock with blocking acquire."""
    from src.scheduler.resource_registry import ResourceRegistry
    ResourceRegistry.enable_file_lock()
    reg = ResourceRegistry()
    reg._reset_for_test()

    t0 = time.time()
    meta = {"plan_id": f"proc-{os.getpid()}"}
    # Use blocking acquire (resilient to race conditions)
    ok = reg.wait_and_hold(ekey, meta, timeout=10)
    if ok:
        time.sleep(0.5)
        reg.release(ekey)
    elapsed = time.time() - t0
    result_queue.put({"acquired": ok, "elapsed": elapsed})


def test_registry_file_lock_two_processes():
    """Two processes using ResourceRegistry+FileLock → serial."""
    ekey = "BMC:10.0.0.96:443"

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()

    t0 = time.time()
    p1 = ctx.Process(target=_registry_subprocess_worker, args=(ekey, q))
    p2 = ctx.Process(target=_registry_subprocess_worker, args=(ekey, q))
    p1.start()
    time.sleep(0.05)
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = []
    while not q.empty():
        results.append(q.get())

    elapsed = time.time() - t0
    assert len(results) == 2
    acquired_count = sum(1 for r in results if r["acquired"])
    assert acquired_count == 2, f"Both should acquire eventually, got {acquired_count}"
    assert elapsed >= 0.9, f"File lock + registry not serializing: {elapsed:.2f}s"

    print(f"  PASS: registry+file_lock two processes — wall_clock={elapsed:.2f}s")


# ===================================================================
# Main
# ===================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
