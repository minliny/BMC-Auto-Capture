"""NP-001~004: Network preflight endpoint dedup and unified implementation tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.device import Device
from src.connectivity.preflight import (
    check_all, apply_preflight, PreflightStatus, PreflightReport,
)


def _make_device(name, bmc_ip="", inband_ip="", group="", enabled=True):
    return Device(
        row_index=0,
        device_name=name,
        device_group=group,
        bmc_ip=bmc_ip,
        inband_ip=inband_ip,
        enabled=enabled,
        bmc_username="admin",
        bmc_password="pass",
        inband_username="root",
        inband_password="pass",
    )


class TestPreflightEndpointDedup:
    """NP-002: Preflight probes by endpoint, not by device_name."""

    @patch("src.connectivity.preflight._tcp_probe")
    def test_same_device_multiple_tasks_probed_once(self, mock_probe):
        """Same device_name with multiple tasks: only probe once per endpoint."""
        mock_probe.return_value = (PreflightStatus.OK, "", 1.0)
        d = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        report = check_all([d, d])  # same device twice
        assert mock_probe.call_count == 2  # one BMC + one SSH
        assert report.probe_count == 2

    @patch("src.connectivity.preflight._tcp_probe")
    def test_different_device_same_host_port_probed_once(self, mock_probe):
        """Different device_names but same host:port: only probe once per endpoint."""
        mock_probe.return_value = (PreflightStatus.OK, "", 1.0)
        d1 = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        d2 = _make_device("dev2", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        report = check_all([d1, d2])
        assert mock_probe.call_count == 2  # one BMC + one SSH (shared)
        assert report.probe_count == 2
        assert report.impacted_task_count == 4  # 2 devices × 2 endpoints each

    @patch("src.connectivity.preflight._tcp_probe")
    def test_same_host_different_port_separate_probe(self, mock_probe):
        """Same host but different port: separate probes."""
        mock_probe.return_value = (PreflightStatus.OK, "", 1.0)
        d1 = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.1")
        report = check_all([d1])
        assert mock_probe.call_count == 2  # BMC:443 and SSH:22

    @patch("src.connectivity.preflight._tcp_probe")
    def test_bmc_ssh_same_ip_different_protocol(self, mock_probe):
        """BMC and SSH on same IP: separate probes (different protocol/port)."""
        mock_probe.return_value = (PreflightStatus.OK, "", 1.0)
        d = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.1")
        report = check_all([d])
        assert mock_probe.call_count == 2

    @patch("src.connectivity.preflight._tcp_probe")
    def test_empty_ip_generates_ip_empty_no_probe(self, mock_probe):
        """Empty IP generates IP_EMPTY status without socket probe."""
        d = _make_device("dev1", bmc_ip="", inband_ip="")
        report = check_all([d])
        mock_probe.assert_not_called()
        assert report.probe_count == 2  # still counted as endpoints
        assert report.results[0].bmc_status == PreflightStatus.IP_EMPTY
        assert report.results[0].ssh_status == PreflightStatus.IP_EMPTY


class TestPreflightLogging:
    """NP-003: Logs distinguish probe count and impacted task count."""

    @patch("src.connectivity.preflight._tcp_probe")
    def test_log_contains_probe_and_task_count(self, mock_probe, caplog):
        """Log must include probe count and impacted task count."""
        mock_probe.return_value = (PreflightStatus.OK, "", 1.0)
        d1 = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        d2 = _make_device("dev2", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        with caplog.at_level("INFO"):
            report = check_all([d1, d2])
        assert "探测端点" in caplog.text
        assert "影响任务" in caplog.text
        assert "跳过任务" in caplog.text
        assert report.probe_count == 2
        assert report.impacted_task_count == 4


class TestPreflightApply:
    """NP-001: launcher preview does NOT call apply_preflight; formal execution does."""

    def test_apply_preflight_per_task(self):
        """apply_preflight applies results per-task, sharing endpoint results."""
        from src.models.task_plan import TaskPlan
        from src.models.task import Task

        d1 = _make_device("dev1", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")
        d2 = _make_device("dev2", bmc_ip="10.0.0.1", inband_ip="10.0.0.2")

        report = PreflightReport(
            results=[
                MagicMock(
                    device_name="dev1",
                    bmc_status=PreflightStatus.UNREACHABLE,
                    bmc_error="网络不可达",
                    ssh_status=PreflightStatus.OK,
                    ssh_error="",
                ),
                MagicMock(
                    device_name="dev2",
                    bmc_status=PreflightStatus.UNREACHABLE,
                    bmc_error="网络不可达",
                    ssh_status=PreflightStatus.OK,
                    ssh_error="",
                ),
            ],
        )

        t = Task(row_index=1, sequence=1, task_name="t1", task_type="BMC",
                 execution_mode="BMC_URL", command_or_url="https://10.0.0.1")
        p1 = TaskPlan(device=d1, task=t)
        p2 = TaskPlan(device=d2, task=t)

        result = apply_preflight([p1, p2], report)
        assert p1.status.startswith("EXEC_SKIPPED")
        assert p2.status.startswith("EXEC_SKIPPED")
