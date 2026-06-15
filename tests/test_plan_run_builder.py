from __future__ import annotations

from dataclasses import dataclass

from src.plan_run_service.builder import PlanRunBuilder, derive_lock_uri


@dataclass
class FakeDevice:
    device_name: str
    device_group: str
    bmc_ip: str = ""
    inband_ip: str = ""
    enabled: bool = True


@dataclass
class FakeTask:
    task_name: str
    task_type: str
    execution_mode: str
    match_group: str = ""
    enabled: bool = True


def _item_factory(**kwargs):
    return kwargs


def test_plan_run_builder_filters_enabled_and_match_group():
    devices = [
        FakeDevice("A3-device", "A3", bmc_ip="10.0.0.1"),
        FakeDevice("L1-device", "L1", inband_ip="192.0.2.1"),
        FakeDevice("disabled", "A3", enabled=False),
    ]
    tasks = [
        FakeTask("bmc", "BMC", "BMC_URL", match_group="A3"),
        FakeTask("ssh", "SSH", "SSH_CMD", match_group="L1/L2"),
        FakeTask("disabled-task", "BMC", "BMC_URL", enabled=False),
    ]

    items = PlanRunBuilder().build_items("plan-1", devices, tasks, item_factory=_item_factory)

    assert [(item["device_name"], item["task_name"]) for item in items] == [
        ("A3-device", "bmc"),
        ("L1-device", "ssh"),
    ]
    assert items[0]["lock_uri"] == "bmc://10.0.0.1"
    assert items[1]["lock_uri"] == "ssh-vrp://192.0.2.1"
    assert items[0]["_device"] is devices[0]
    assert items[0]["_task"] is tasks[0]


def test_derive_lock_uri_ssh_linux_and_missing_ip():
    linux_device = FakeDevice("linux", "A3", inband_ip="192.0.2.10")
    vrp_device = FakeDevice("vrp", "L2", inband_ip="192.0.2.11")
    missing_ip = FakeDevice("missing", "A3")
    ssh_task = FakeTask("ssh", "SSH", "SSH_CMD")

    assert derive_lock_uri(linux_device, ssh_task) == "ssh-linux://192.0.2.10"
    assert derive_lock_uri(vrp_device, ssh_task) == "ssh-vrp://192.0.2.11"
    assert derive_lock_uri(missing_ip, ssh_task) == ""
