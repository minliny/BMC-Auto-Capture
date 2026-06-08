#!/usr/bin/env python3
"""CLI/mock tests for --preflight-auth (no real HW).

Run: python tests/test_preflight_auth_cli.py
"""

from __future__ import annotations

import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
# Test 1: CLI --help includes --preflight-auth
# ---------------------------------------------------------------------------
def test_cli_help_run_py():
    print("\n── CLI help: run.py --help ──")
    proj = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(proj / "run.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    check("exit 0", result.returncode == 0, str(result.returncode))
    check("--preflight-auth in help",
          "--preflight-auth" in result.stdout,
          "Missing from run.py --help output")
    check("--preflight-target in help",
          "--preflight-target" in result.stdout)


def test_cli_help_main_py():
    print("\n── CLI help: __main__.py --help ──")
    proj = Path(__file__).resolve().parent.parent
    # Check that --preflight-auth is defined in the argparse setup of __main__.py
    with open(proj / "src" / "__main__.py") as f:
        content = f.read()
    check("--preflight-auth arg defined in __main__.py",
          "--preflight-auth" in content and "choices=" in content,
          "__main__.py missing --preflight-auth argparse definition")
    check("--preflight-target arg defined in __main__.py",
          "--preflight-target" in content)


def test_cli_invalid_auth_arg():
    print("\n── CLI: invalid --preflight-auth value ──")
    proj = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(proj / "run.py"),
         "--preflight-auth", "invalid_value"],
        capture_output=True, text=True, timeout=10,
    )
    check("exit != 0 on invalid", result.returncode != 0,
          f"rc={result.returncode}")


# ---------------------------------------------------------------------------
# Test 2: Mock auth checks
# ---------------------------------------------------------------------------
def test_mock_auth_checks():
    print("\n── Mock auth checks ──")
    from src.models.device import Device
    from src.connectivity.preflight import (
        _check_bmc_auth, _check_ssh_auth, check_auth_all, PreflightReport, PreflightResult
    )

    # 2a: BMC with valid-looking IP (will fail connection, but that's expected)
    d_bmc = Device(0, "D1", "G1", "10.255.255.254", "admin", "pass",
                   enabled=True)
    status, err, dur = _check_bmc_auth(d_bmc, timeout=0.5)
    check("BMC check returns status", status in (
        "AUTH_OK", "AUTH_FAILED", "TIMEOUT", "CONNECT_FAILED",
        "ERROR", "IP_EMPTY", "CREDENTIAL_EMPTY",
    ), status)
    check("BMC check has duration", dur >= 0)

    # 2b: BMC IP empty
    d_empty = Device(0, "D2", "G1", "", "", "", enabled=True)
    s, e, d = _check_bmc_auth(d_empty, timeout=1)
    check("BMC IP empty → IP_EMPTY", s == "IP_EMPTY", f"got {s}")

    # 2c: BMC credential empty
    d_cred = Device(0, "D3", "G1", "10.0.0.1", "", "", enabled=True)
    s, e, d = _check_bmc_auth(d_cred, timeout=0.5)
    check("BMC cred empty → CREDENTIAL_EMPTY", s == "CREDENTIAL_EMPTY",
          f"got {s}: {e}")

    # 2d: SSH IP empty
    d_ssh_empty = Device(0, "D4", "G1", "", "", "",
                         inband_ip="", inband_username="u", inband_password="p",
                         enabled=True)
    s, e, d = _check_ssh_auth(d_ssh_empty, timeout=0.5)
    check("SSH IP empty → IP_EMPTY", s == "IP_EMPTY", f"got {s}")

    # 2e: SSH credential empty
    d_ssh_cred = Device(0, "D5", "G1", "", "", "",
                        inband_ip="10.0.0.1", inband_username="", inband_password="",
                        enabled=True)
    s, e, d = _check_ssh_auth(d_ssh_cred, timeout=0.5)
    check("SSH cred empty → CREDENTIAL_EMPTY",
          s == "CREDENTIAL_EMPTY", f"got {s}: {e}")


# ---------------------------------------------------------------------------
# Test 3: PreflightReport + CSV writer
# ---------------------------------------------------------------------------
def test_auth_csv_writer():
    print("\n── Auth CSV writer ──")
    import tempfile
    from src.connectivity.preflight import PreflightResult, PreflightReport
    from src.out.collector import write_preflight_auth_csv

    results = [
        PreflightResult(
            device_name="D1", device_group="G1",
            bmc_status="AUTH_OK", bmc_endpoint="10.0.0.1:443",
            bmc_username="admin", bmc_duration=0.5,
            ssh_status="IP_EMPTY", ssh_error="无带内IP",
        ),
        PreflightResult(
            device_name="D2", device_group="G1",
            bmc_status="CONNECT_FAILED", bmc_endpoint="10.0.0.2:443",
            bmc_username="admin", bmc_error="连接拒绝", bmc_duration=1.2,
            ssh_status="AUTH_OK", ssh_endpoint="192.168.1.1:22",
            ssh_username="root", ssh_duration=0.3,
        ),
    ]
    report = PreflightReport(results=results, total=2,
                             bmc_ok=1, bmc_fail=1, ssh_ok=1, ssh_fail=1)

    tmp = tempfile.mkdtemp()
    p = write_preflight_auth_csv(report, tmp)
    check("CSV created", __import__('os').path.exists(p))

    with open(p) as f:
        content = f.read()
    check("contains AUTH_OK", "AUTH_OK" in content)
    check("contains CONNECT_FAILED", "CONNECT_FAILED" in content)
    check("contains IP_EMPTY", "IP_EMPTY" in content)
    check("contains device_name", "D1" in content and "D2" in content)

    import shutil
    shutil.rmtree(tmp)


# ================================================================
if __name__ == "__main__":
    test_cli_help_run_py()
    test_cli_help_main_py()
    test_cli_invalid_auth_arg()
    test_mock_auth_checks()
    test_auth_csv_writer()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
