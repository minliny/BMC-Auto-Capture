#!/usr/bin/env python3
"""Offline verification suite — no pytest, no real BMC/SSH hardware.

Runs all standalone tests in sequence.  Designed for Windows environments
where pytest may not be available.

Usage:
  python verify_offline.py              # run all
  python verify_offline.py --quick      # skip subprocess tests
  python verify_offline.py --list       # list available suites

Exit code: 0 = all passed, 1 = at least one failure.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent
TESTS_DIR = PROJ_ROOT / "tests"

# Suite definitions: (name, script_path, requires_subprocess)
SUITES = [
    ("endpoint_key",        TESTS_DIR / "test_endpoint_key.py",                        False),
    ("resource_registry",   TESTS_DIR / "test_resource_registry.py",                   False),
    ("file_lock_windows",   TESTS_DIR / "test_file_lock_windows.py",                   True),
    ("api_run_with_plans",  TESTS_DIR / "test_api_run_with_plans_source_or_mock.py",   False),
]


def run_script(path: Path) -> tuple[bool, str, float]:
    """Run a single test script. Returns (passed, output_tail, elapsed)."""
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJ_ROOT),
        )
        elapsed = time.time() - t0
        passed = proc.returncode == 0
        tail = proc.stdout.split("\n")[-5:] if proc.stdout else ["(no output)"]
        if proc.stderr and not passed:
            tail = (proc.stderr.split("\n")[-3:] or tail)
        return passed, "\n".join(tail), elapsed
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT (>120s)", time.time() - t0
    except Exception as e:
        return False, str(e), time.time() - t0


def main():
    quick = "--quick" in sys.argv
    list_only = "--list" in sys.argv

    if list_only:
        print("Available offline verification suites:")
        for name, path, needs_sp in SUITES:
            tag = " [subprocess]" if needs_sp else ""
            print(f"  {name}{tag}  →  {path.name}")
        return

    to_run = SUITES
    if quick:
        to_run = [(n, p, sp) for n, p, sp in SUITES if not sp]
        print("(quick mode: skipping subprocess tests)\n")

    print(f"{'=' * 60}")
    print(f"  Offline Verification Suite")
    print(f"  Platform: {sys.platform}   Python: {sys.version.split()[0]}")
    print(f"  Suites: {len(to_run)}")
    print(f"{'=' * 60}")

    results = []
    for name, path, needs_sp in to_run:
        tag = " [subprocess]" if needs_sp else ""
        print(f"\n── {name}{tag} ──")
        passed, tail, elapsed = run_script(path)
        results.append((name, passed, elapsed))
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}  ({elapsed:.1f}s)")
        if not passed:
            for line in tail:
                print(f"    {line}")

    # Summary
    print(f"\n{'=' * 60}")
    total = len(results)
    passed = sum(1 for _, p, _ in results if p)
    failed = total - passed
    for name, ok, dt in results:
        status = "OK" if ok else "FAIL"
        print(f"  {status:>4}  {name:<25s}  {dt:.1f}s")

    print(f"\n  {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
