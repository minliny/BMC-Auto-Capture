#!/usr/bin/env python3
"""Standalone endpoint_key integrity tests (no pytest, no HW).

Validates:
  - BMC:   BMC:<OOB_IP>:443
  - SSH:   INBAND:<IB_IP>:22
  - TELNET: INBAND:<IB_IP>:23
  - Missing IP → MISSING_IP fallback
  - device_id backward compat

Run: python tests/test_endpoint_key.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan

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


def test_bmc_endpoint_key():
    print("\n── BMC endpoint_key ──")
    d = Device(0, "D1", "G1", "10.0.0.1", "u1", "p1")
    t = Task(0, 0, "BMC_T", "BMC", "BMC_URL", command_or_url="/test")
    p = TaskPlan(device=d, task=t)

    check("endpoint_type == BMC", p.endpoint_type == "BMC")
    check("endpoint_key  == BMC:10.0.0.1:443", p.endpoint_key == "BMC:10.0.0.1:443",
          f"got {p.endpoint_key}")
    check("resource_type == BMC", p.resource_type == "BMC")
    check("device_id == D1", p.device_id == "D1")


def test_ssh_endpoint_key():
    print("\n── SSH endpoint_key ──")
    d = Device(0, "D2", "G1", "", "", "",
               inband_ip="192.168.1.1",
               inband_username="u", inband_password="p")
    t = Task(0, 0, "SSH_T", "SSH", "SSH_CMD", command_or_url="show")
    p = TaskPlan(device=d, task=t)

    check("endpoint_type == INBAND", p.endpoint_type == "INBAND")
    check("endpoint_key  == INBAND:192.168.1.1:22", p.endpoint_key == "INBAND:192.168.1.1:22",
          f"got {p.endpoint_key}")
    check("resource_type == INBAND", p.resource_type == "INBAND")


def test_telnet_endpoint_key():
    print("\n── TELNET endpoint_key ──")
    d = Device(0, "D3", "G1", "", "", "",
               inband_ip="10.0.0.99",
               inband_username="u", inband_password="p")
    t = Task(0, 0, "TELNET_T", "TELNET", "TELNET_CMD", command_or_url="disp ver")
    p = TaskPlan(device=d, task=t)

    check("endpoint_type == INBAND", p.endpoint_type == "INBAND",
          f"got {p.endpoint_type}")
    check("endpoint_key  == INBAND:10.0.0.99:23", p.endpoint_key == "INBAND:10.0.0.99:23",
          f"got {p.endpoint_key} (port must be 23, NOT 22)")
    check("resource_type == INBAND", p.resource_type == "INBAND")


def test_missing_ip_fallback():
    print("\n── Missing IP fallback ──")

    d1 = Device(0, "D4", "G1", "", "", "")
    t1 = Task(0, 0, "BMC_T", "BMC", "BMC_URL", command_or_url="/test")
    p1 = TaskPlan(device=d1, task=t1)
    check("BMC  no IP → MISSING_IP in key", "MISSING_IP" in p1.endpoint_key,
          p1.endpoint_key)
    check("BMC  no IP → contains device_name", "D4" in p1.endpoint_key)

    d2 = Device(0, "D5", "G1", "", "", "",
                inband_ip="", inband_username="u", inband_password="p")
    t2 = Task(0, 0, "SSH_T", "SSH", "SSH_CMD", command_or_url="show")
    p2 = TaskPlan(device=d2, task=t2)
    check("INBAND no IP → MISSING_IP in key", "MISSING_IP" in p2.endpoint_key,
          p2.endpoint_key)
    check("INBAND no IP → contains device_name", "D5" in p2.endpoint_key)

    d3 = Device(0, "D6", "G1", "", "", "",
                inband_ip="", inband_username="u", inband_password="p")
    t3 = Task(0, 0, "TELNET_T", "TELNET", "TELNET_CMD", command_or_url="disp")
    p3 = TaskPlan(device=d3, task=t3)
    check("TELNET no IP → MISSING_IP in key", "MISSING_IP" in p3.endpoint_key,
          p3.endpoint_key)
    check("TELNET no IP → contains device_name", "D6" in p3.endpoint_key)


def test_device_id_backward_compat():
    print("\n── device_id backward compat ──")
    d = Device(0, "MyDevice", "G1", "10.0.0.1", "u", "p", "", "", "")
    t = Task(0, 0, "T1", "BMC", "BMC_URL", command_or_url="/test")
    p = TaskPlan(device=d, task=t)
    check("device_id == device.device_name", p.device_id == "MyDevice")


# ================================================================
if __name__ == "__main__":
    test_bmc_endpoint_key()
    test_ssh_endpoint_key()
    test_telnet_endpoint_key()
    test_missing_ip_fallback()
    test_device_id_backward_compat()

    print(f"\n{'=' * 50}")
    if FAILS == 0:
        print(f"  ALL {TOTAL} PASSED")
        sys.exit(0)
    else:
        print(f"  {FAILS}/{TOTAL} FAILED")
        sys.exit(1)
