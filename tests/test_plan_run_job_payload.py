from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.plan_run_service.job_payload import PlanRunJobPayloadBuilder
from src.plan_run_service.service import PlanRunItem


@dataclass
class FakeDevice:
    device_name: str = "device-1"
    device_group: str = "L1"
    bmc_ip: str = "10.0.0.1"
    bmc_username: str = "admin"
    bmc_password: str = "env:BMC_PASSWORD"
    inband_ip: str = "192.0.2.10"
    inband_username: str = "root"
    inband_password: str = "plain-ssh-password"


@dataclass
class FakeTask:
    task_id: str = "task.ssh.version"
    task_name: str = "task-1"
    task_type: str = "SSH"
    execution_mode: str = "SSH_CMD"
    command_or_url: str = "show version"
    sequence: int = 7
    sequence_str: str = "007"
    match_group: str = "L1"
    timeout_seconds: int = 30
    retry_count: int = 1
    _per_group_commands: dict[str, str] = field(default_factory=lambda: {"L1": "display version"})
    _per_group_no_split: dict[str, bool] = field(default_factory=lambda: {"L1": True})
    _per_group_timeout_seconds: dict[str, int] = field(default_factory=lambda: {"L1": 45})
    _no_split: bool = True


def test_job_payload_builder_preserves_ssh_overrides_and_metadata():
    item = PlanRunItem(
        plan_id="plan-1",
        device_group="L1",
        device_name="device-1",
        task_name="task-1",
        task_id="task.ssh.version",
        plan_item_id="plan-1:device-1:task.ssh.version",
        _device=FakeDevice(),
        _task=FakeTask(),
    )

    payload = PlanRunJobPayloadBuilder().build(item)

    assert payload["job_id"] == "plan-1:device-1:task.ssh.version"
    assert payload["plan_id"] == "plan-1"
    assert payload["task_snapshot"]["task_id"] == "task.ssh.version"
    assert payload["task_snapshot"]["plan_item_id"] == "plan-1:device-1:task.ssh.version"
    assert payload["device_snapshot"]["ssh_type"] == "SSH_VRP"
    assert payload["device_snapshot"]["inband_password_ref"] == "plain-ssh-password"
    assert payload["task_snapshot"]["command_or_url"] == "display version"
    assert payload["task_snapshot"]["raw_command_or_url"] == "show version"
    assert payload["task_snapshot"]["ssh_profile"] == "vrp"
    assert payload["task_snapshot"]["ssh_evidence_mode"] == "terminal"
    assert payload["task_snapshot"]["per_group_no_split"] == {"L1": True}
    assert payload["task_snapshot"]["per_group_timeout_seconds"] == {"L1": 45}
    assert payload["task_snapshot"]["no_split"] is True


def test_job_payload_builder_uses_linux_profile_for_non_l1_l2():
    device = FakeDevice(device_group="A3")
    task = FakeTask(_per_group_commands={})
    item = PlanRunItem(
        plan_id="plan-1",
        device_group="A3",
        device_name="device-1",
        task_name="task-1",
        _device=device,
        _task=task,
    )

    payload = PlanRunJobPayloadBuilder().build(item)

    assert payload["device_snapshot"]["ssh_type"] == "SSH_LINUX"
    assert payload["task_snapshot"]["ssh_profile"] == "linux"
    assert payload["task_snapshot"]["command_or_url"] == "show version"


def test_job_payload_builder_requires_device_and_task_refs():
    item = PlanRunItem(plan_id="plan-1", device_name="device-1", task_name="task-1")

    with pytest.raises(ValueError, match="Missing device or task reference"):
        PlanRunJobPayloadBuilder().build(item)
