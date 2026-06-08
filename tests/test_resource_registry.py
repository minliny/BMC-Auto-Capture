#!/usr/bin/env python3
"""Standalone ResourceRegistry tests (no pytest, no HW).

Validates:
  - try_hold / release
  - reentrant acquire
  - cross-scheduler serialization (thread-based)
  - is_held / held_keys
  - FileLock integration (if available on platform)

Run: python tests/test_resource_registry.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def test_try_hold_release():
    print("\n── try_hold / release ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    ekey = "BMC:10.0.0.1:443"

    ok = reg.try_hold(ekey, {"plan_id": "p1", "device_name": "D1", "task_name": "T1"})
    check("try_hold free key", ok)
    check("is_held after acquire", reg.is_held(ekey))
    check("held_keys contains key", ekey in reg.held_keys)

    ok2 = reg.try_hold(ekey, {"plan_id": "p2"})
    check("try_hold held key → False", not ok2)

    reg.release(ekey)
    check("is_held after release → False", not reg.is_held(ekey))
    check("held_keys empty", len(reg.held_keys) == 0)

    # Re-acquire after release
    ok3 = reg.try_hold(ekey, {"plan_id": "p3"})
    check("re-acquire after release", ok3)
    reg.release(ekey)


def test_reentrant_acquire():
    print("\n── reentrant acquire ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    ekey = "BMC:10.0.0.2:443"
    meta = {"execution_id": "e1", "plan_id": "p1",
            "device_name": "D1", "task_name": "T1"}

    with reg.acquire(ekey, meta) as lease:
        check("first acquire not reentrant", not lease["reentrant"])
        with reg.acquire(ekey, meta) as lease2:
            check("second acquire IS reentrant", lease2["reentrant"])
            check("reentrant wait_seconds == 0", lease2["wait_seconds"] == 0.0)
        check("still held after reentrant exit", reg.is_held(ekey))
    check("released after outer exit", not reg.is_held(ekey))


def test_cross_thread_serialization():
    print("\n── cross-thread serialization ──")
    reg = ResourceRegistry()
    reg._reset_for_test()
    ekey = "BMC:10.0.0.3:443"

    order = []
    def worker(name: str):
        meta = {"plan_id": name, "device_name": name, "task_name": "T1"}
        with reg.acquire(ekey, meta):
            order.append(name)
            time.sleep(0.3)

    t0 = time.time()
    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.05)  # Ensure t1 acquires first
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    elapsed = time.time() - t0

    check("both executed", len(order) == 2, f"got {order}")
    check("serial (>=0.5s)", elapsed >= 0.5, f"elapsed={elapsed:.2f}s")
    check("not held after all done", not reg.is_held(ekey))


def test_file_lock_integration():
    print("\n── FileLock integration ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    # Enable file lock
    ResourceRegistry.enable_file_lock()

    ekey = "BMC:10.0.0.99:443"
    ok1 = reg.try_hold(ekey, {"plan_id": "p1"})
    check("try_hold with file lock", ok1)

    ok2 = reg.try_hold(ekey, {"plan_id": "p2"})
    check("try_hold blocked by file lock", not ok2)

    reg.release(ekey)
    check("released after file lock", not reg.is_held(ekey))

    # Clean up lock files
    try:
        from src.scheduler.file_lock import FileLock
        fl = FileLock()
        lp = fl._lock_path(ekey)
        if lp.exists():
            lp.unlink()
        ip = fl._lock_info_path(ekey)
        if ip.exists():
            ip.unlink()
    except Exception:
        pass


def test_different_keys_independent():
    print("\n── different keys independent ──")
    reg = ResourceRegistry()
    reg._reset_for_test()

    check("key A free", reg.try_hold("BMC:10.0.0.10:443", {"plan_id": "a"}))
    check("key B free while A held", reg.try_hold("BMC:10.0.0.11:443", {"plan_id": "b"}))
    check("key C free while A+B held", reg.try_hold("INBAND:192.168.1.1:22", {"plan_id": "c"}))

    reg.release("BMC:10.0.0.10:443")
    reg.release("BMC:10.0.0.11:443")
    reg.release("INBAND:192.168.1.1:22")
    check("all released", reg.active_lease_count == 0)


# ================================================================
if __name__ == "__main__":
    test_try_hold_release()
    test_reentrant_acquire()
    test_cross_thread_serialization()
    test_different_keys_independent()
    test_file_lock_integration()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
