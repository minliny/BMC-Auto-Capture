#!/usr/bin/env python3
"""Standalone cross-process file lock tests (no pytest, no HW).

Validates on both Windows (msvcrt) and Unix (fcntl):
  - acquire / release
  - try_acquire blocking
  - same endpoint serial (subprocess)
  - different endpoint concurrent (subprocess)
  - safe_filename encoding
  - lock file cleanup

Run: python tests/test_file_lock_windows.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scheduler.file_lock import FileLock, _safe_filename

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


def test_safe_filename():
    print("\n── safe_filename ──")
    check("BMC:10.0.0.1:443",
          _safe_filename("BMC:10.0.0.1:443") == "BMC_10.0.0.1_443")
    check("INBAND:192.168.1.1:22",
          _safe_filename("INBAND:192.168.1.1:22") == "INBAND_192.168.1.1_22")
    check("TELNET :23",
          _safe_filename("INBAND:10.0.0.1:23") == "INBAND_10.0.0.1_23")
    check("MISSING_IP",
          _safe_filename("BMC:MISSING_IP:D1") == "BMC_MISSING_IP_D1")
    # Path traversal prevention
    safe = _safe_filename("../etc/passwd")
    check("no .. traversal", ".." not in safe, safe)


def test_acquire_release():
    print("\n── acquire/release ──")
    lock = FileLock()
    ekey = "BMC:10.0.0.77:443"

    check("not locked before", not lock.is_locked(ekey))
    with lock.acquire(ekey, timeout=2):
        check("locked during", lock.is_locked(ekey))
    check("not locked after", not lock.is_locked(ekey))


def test_try_acquire():
    print("\n── try_acquire ──")
    lock = FileLock()
    ekey = "BMC:10.0.0.78:443"

    ctx = lock.try_acquire(ekey)
    check("try_acquire free → success", ctx is not None)
    check("locked after try_acquire", lock.is_locked(ekey))

    ctx2 = lock.try_acquire(ekey)
    check("try_acquire held → None", ctx2 is None)

    ctx.__exit__()
    check("unlocked after ctx exit", not lock.is_locked(ekey))


def test_different_keys_independent():
    print("\n── different keys independent ──")
    lock = FileLock()
    key_a = "BMC:10.0.0.81:443"
    key_b = "BMC:10.0.0.82:443"

    with lock.acquire(key_a, timeout=2):
        check("key A locked", lock.is_locked(key_a))
        ctx_b = lock.try_acquire(key_b)
        check("key B free while A locked", ctx_b is not None)
        if ctx_b:
            ctx_b.__exit__()
    check("key A released", not lock.is_locked(key_a))


def test_lock_file_cleanup():
    print("\n── lock file cleanup ──")
    tmp = tempfile.mkdtemp(prefix="bmc_test_cleanup_")
    lock = FileLock(lock_dir=tmp)
    ekey = "BMC:10.0.0.79:443"
    lp = lock._lock_path(ekey)

    with lock.acquire(ekey, timeout=2):
        check("lock file created", lp.exists(),
              f"expected at {lp}")

    check("lock file removed after release", not lp.exists(),
          f"still exists at {lp}")


def test_subprocess_same_endpoint_serial():
    print("\n── subprocess same endpoint serial ──")
    import subprocess

    tmp_dir = tempfile.mkdtemp(prefix="bmc_flock_test_")
    ekey = "BMC:10.0.0.99:443"

    worker_script = f"""
import sys, time
sys.path.insert(0, r'{Path(__file__).resolve().parent.parent}')
from src.scheduler.file_lock import FileLock
lock = FileLock(r'{tmp_dir}')
t0 = time.time()
with lock.acquire("{ekey}", timeout=10):
    time.sleep(0.5)
print(f"DONE={{time.time() - t0:.3f}}")
"""

    worker_path = os.path.join(tmp_dir, "_worker.py")
    with open(worker_path, "w") as f:
        f.write(worker_script)

    t0 = time.time()
    p1 = subprocess.Popen([sys.executable, worker_path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.1)  # Let p1 acquire first
    p2 = subprocess.Popen([sys.executable, worker_path],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out1, _ = p1.communicate(timeout=10)
    out2, _ = p2.communicate(timeout=10)
    elapsed = time.time() - t0

    check("both completed", p1.returncode == 0 and p2.returncode == 0,
          f"rc1={p1.returncode} rc2={p2.returncode}")
    check("serial (>=0.9s)", elapsed >= 0.9, f"elapsed={elapsed:.2f}s")

    # Cleanup
    import shutil
    try:
        shutil.rmtree(tmp_dir)
    except Exception:
        pass


def test_subprocess_different_endpoint_concurrent():
    print("\n── subprocess different endpoint concurrent ──")
    import subprocess

    tmp_dir = tempfile.mkdtemp(prefix="bmc_flock_concurrent_")

    worker_script_tpl = """
import sys, time
sys.path.insert(0, r'{proj_root}')
from src.scheduler.file_lock import FileLock
lock = FileLock(r'{lock_dir}')
t0 = time.time()
with lock.acquire("{ekey}", timeout=10):
    time.sleep(0.5)
print(f"DONE={{time.time() - t0:.3f}}")
"""

    proj = str(Path(__file__).resolve().parent.parent)
    wa = worker_script_tpl.format(proj_root=proj, lock_dir=tmp_dir,
                                  ekey="BMC:10.0.0.98:443")
    wb = worker_script_tpl.format(proj_root=proj, lock_dir=tmp_dir,
                                  ekey="BMC:10.0.0.97:443")

    pa = os.path.join(tmp_dir, "_worker_a.py")
    pb = os.path.join(tmp_dir, "_worker_b.py")
    with open(pa, "w") as f:
        f.write(wa)
    with open(pb, "w") as f:
        f.write(wb)

    t0 = time.time()
    p1 = subprocess.Popen([sys.executable, pa],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen([sys.executable, pb],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p1.communicate(timeout=10)
    p2.communicate(timeout=10)
    elapsed = time.time() - t0

    check("both ok", p1.returncode == 0 and p2.returncode == 0)
    check("concurrent (<1.5s)", elapsed < 1.5, f"elapsed={elapsed:.2f}s")

    import shutil
    try:
        shutil.rmtree(tmp_dir)
    except Exception:
        pass


# ================================================================
if __name__ == "__main__":
    print(f"Platform: {sys.platform}")
    test_safe_filename()
    test_acquire_release()
    test_try_acquire()
    test_different_keys_independent()
    test_lock_file_cleanup()
    test_subprocess_same_endpoint_serial()
    test_subprocess_different_endpoint_concurrent()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
