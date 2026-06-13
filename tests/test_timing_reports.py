"""E2E timing report validation with fake executor (no real browser).

Run: python -m pytest tests/test_timing_reports.py -v
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.models.device import Device
from src.models.task import Task
from src.models.task_plan import TaskPlan
from src.models.execution_result import ExecutionResult
from src.models.app_config import AppConfig
from src.scheduler.resource_registry import ResourceRegistry
from src.out.timing import write_all_timing_reports


@pytest.fixture(autouse=True)
def reset_and_mock(monkeypatch):
    """Reset registry + mock BMC session runner + SSH executor."""
    reg = ResourceRegistry()
    reg._reset_for_test()

    # Mock BMC session runner (no browser)
    import src.scheduler.bmc_session_runner as bsr
    from tests.fakes import FakeBMCSessionRunner
    monkeypatch.setattr(bsr, "BMCEndpointSessionRunner", lambda **kw: FakeBMCSessionRunner(
        browser_manager=None, endpoint_key=kw.get("endpoint_key", ""),
        plans=kw.get("plans", []), output_root=kw.get("output_root", ""),
        on_plan_done=kw.get("on_plan_done"),
        on_group_done=kw.get("on_group_done"),
    ))

    # Mock SSH executor (no paramiko)
    from tests.fakes import FakeSSHExecutor
    import src.scheduler.dynamic_scheduler as dynamic_scheduler
    class _MockSSH(FakeSSHExecutor):
        pass
    monkeypatch.setattr(
        dynamic_scheduler,
        "SSHExecutor",
        lambda *a, **kw: _MockSSH(
            sleep_seconds=0.05, result_status="EXEC_SUCCESS"
        ),
    )

    yield
    reg._reset_for_test()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_device(name, bmc_ip="", inband_ip=""):
    return Device(0, name, "G1", bmc_ip, "u", "p",
                  inband_ip=inband_ip, inband_username="u", inband_password="p",
                  enabled=True)


def _make_task(name, task_type="BMC", exec_mode="BMC_URL"):
    return Task(0, 0, name, task_type, exec_mode, command_or_url="/test",
                timeout_seconds=10, enabled=True)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_timing_reports_end_to_end():
    """Full simulation: 6 devices, mix BMC/SSH, verify all 5 report files."""
    plans = [
        TaskPlan(device=_make_device("D1", bmc_ip="10.0.0.1"),
                 task=_make_task("BMC_T1", "BMC")),
        TaskPlan(device=_make_device("D2", bmc_ip="10.0.0.2"),
                 task=_make_task("BMC_T2", "BMC")),
        TaskPlan(device=_make_device("D3", inband_ip="192.168.1.1"),
                 task=_make_task("SSH_T1", "SSH", "SSH_CMD")),
        TaskPlan(device=_make_device("D4", inband_ip="192.168.1.2"),
                 task=_make_task("SSH_T2", "SSH", "SSH_CMD")),
        TaskPlan(device=_make_device("D5", bmc_ip="10.0.0.1"),  # Same BMC IP as D1
                 task=_make_task("BMC_T3", "BMC")),
    ]

    from src.scheduler.dynamic_scheduler import DynamicScheduler
    config = AppConfig()
    config.max_bmc_workers = 3
    config.base_bmc_workers = 3
    config.max_ssh_workers = 3
    config.base_ssh_workers = 3
    config.output_root = "/tmp/bmc_timing_test"

    exec_start = time.time()
    scheduler = DynamicScheduler(config)
    results = scheduler.run(plans)
    wall_clock = time.time() - exec_start

    assert len(results) == 5

    out_dir = tempfile.mkdtemp(prefix="bmc_timing_test_")
    paths = write_all_timing_reports(results, out_dir, execution_started_at=exec_start)

    # --- Verify plan_timing.csv ---
    pt_path = paths["plan_timing"]
    assert os.path.exists(pt_path)
    with open(pt_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 5
    for field in ["plan_id", "device_name", "task_name", "endpoint_key", "duration_seconds"]:
        assert field in rows[0], f"Missing: {field}"
    for r in rows:
        assert float(r["duration_seconds"]) > 0, f"Zero duration: {r}"
    print(f"  PASS: plan_timing.csv — {len(rows)} rows validated")

    # --- Verify device_timing.csv ---
    assert os.path.exists(paths["device_timing"])
    with open(paths["device_timing"], newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5
    print(f"  PASS: device_timing.csv — {len(rows)} rows")

    # --- Verify endpoint_timing.csv ---
    assert os.path.exists(paths["endpoint_timing"])
    with open(paths["endpoint_timing"], newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4  # 4 unique endpoints
    print(f"  PASS: endpoint_timing.csv — {len(rows)} rows")

    # --- Verify execution_summary.json ---
    assert os.path.exists(paths["execution_summary"])
    with open(paths["execution_summary"], encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["total_plans"] == 5
    assert summary["success_count"] == 5
    assert summary["parallel_efficiency"] > 0
    assert summary["wall_clock_seconds"] > 0
    print(f"  PASS: execution_summary.json — parallel_efficiency={summary['parallel_efficiency']:.2f}")

    # --- Verify CSV ---
    assert os.path.exists(os.path.join(out_dir, "execution_summary.csv"))
    print(f"  PASS: execution_summary.csv exists")

    import shutil
    try:
        shutil.rmtree(out_dir)
    except Exception:
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
